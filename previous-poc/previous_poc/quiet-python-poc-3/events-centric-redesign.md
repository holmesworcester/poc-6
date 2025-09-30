# Events-Centric Framework Redesign (Everything Is a Saga)

This document proposes a fully event‑centric framework in which every active component is a saga. Commands, validators, transformers, delta appliers, transport, and query handlers are all sagas that consume events and emit new events. The framework routes events, enforces idempotency, and applies database changes expressed as delta events. There are no envelopes or separate metadata objects — just events.

## Goals

- Single, canonical path: commands produce events; the framework fans those events out to sagas.
- Sagas own coordination logic/state and emit new events; they never directly mutate domain tables (they emit delta events instead).
- Delta events encode DB changes; the framework applies them serially.
- Idempotency, replay, and backfill are first‑class for both Sagas and Projectors.
- Transport/infra concerns (incoming/outgoing/decryption) are modeled as events and handled by Sagas, not by ad‑hoc dict patches.

## Core Concepts

- Event: `{ id: string, type: string, data: object, reply_to?: string }` (no metadata/envelopes). Events may transform as they move through the system.
- Event Store (`event_store`): SQL table storing every event (append‑only). Unique on `id`. Replayable.
- Saga: Subscribes to a filter over new events, uses SQL for state, and emits new events. Projectors are just sagas that emit delta events.
- Delta Events: Event types that encode DB mutations `db.delta/*`; the framework applies these serially.

## Data Model (SQL)

- `event_store(id, event_id UNIQUE, type, data TEXT, created_at_ms, INDEX(type), INDEX(created_at_ms))`
- `saga_applied(saga_name, event_id, applied_at_ms, PRIMARY KEY (saga_name, event_id))`  // exactly‑once per saga
- Domain tables (per protocol), updated only by the delta saga via `db.delta/*`.

Optional:
- `delta_ledger(id, saga_name, event_id, delta JSON, applied_at_ms)` for auditing; not required for correctness.

## Delta Events (Projector Output)

Domain sagas (in the projector role) emit delta events, not SQL. The framework consumes these and mutates the DB.

Event shapes:
- `db.delta/insert`: `{ type: "db.delta/insert", table: "messages", data: {...}, on_conflict: "ignore|update" }`
- `db.delta/update`: `{ type: "db.delta/update", table: "messages", where: {"event_id":"..."}, set: {...} }`
- `db.delta/delete`: `{ type: "db.delta/delete", table: "incoming", where: {"id": 123} }`

Notes:
- Keep the vocabulary small and deterministic; WHERE is equality‑only (AND of fields).
- Framework serializes values and applies deltas inside a single‑writer transaction (delta saga).
- Idempotency: the delta saga records `(saga_name='delta', event_id)` and may guard duplicate effects by `(table, where)`.

## Execution Model

1) Command execution
- A command saga consumes a command event (e.g., `command.message/create`) and emits domain events.
- The framework appends emitted events atomically.

2) Dispatch (No Tick)
- An event queue drives all processing. Appending events triggers delivery to matching sagas and the delta saga (for `db.delta/*`).
- No periodic tick; backpressure is via queue depth and leases.

3) Saga processing
- For each event (or small batch):
  - Begin transaction; check `(saga_name, event_id)`; skip if applied
  - Query any needed state from SQL; no “loaded state” in memory is required
  - Emit 0..N events; append them to `event_store`
  - Record `saga_applied`; commit; yield

4) Delta saga (framework consumer)
- Single‑writer saga that subscribes to `db.delta/*` and applies each delta to SQL in its own small transaction.
- Serial by design; records `(saga_name='delta', event_id)`; yields frequently.

5) Query saga
- Consumes `query.*` events; runs SQL reads; emits `query.response` with `reply_to = <query id>`.

6) Concurrency model
- Same‑kind sagas do not run concurrently (lease per saga_name). Different sagas can run concurrently.
- Assumption: domain events are designed to be commutative; any reordering yields the same end state when queries are applied at the end.
- Counter‑example: `membership.validator` emits add‑user while `group.lifecycle` emits group‑deleted. The delta saga serializes DB effects; constraints may reject late adds — acceptable.

## Roles and Responsibilities

- Sagas: the only active component. They consume events, use SQL reads, and emit new events.
- Framework: appends events, routes to sagas, applies `db.delta/*`, enforces idempotency and fairness. **NOTE:** I don't think the framework applies db.delta. I think this is just another saga. It just happens to control the db the api reads from. (Sagas need to be able to read each others' data sometimes!)

## Transport & Infra as Events (All Events, No Tables)

- Incoming: transport saga emits `incoming.received`; decrypt saga emits `incoming.decrypted`; validator saga emits `message.validated` (or error). No special incoming table is required — everything is events.
- Outgoing: sagas emit `transport.send`; a sender saga integrates with the network and may emit `transport.sent`.
- Purge/removal: modeled as `db.delta/delete` or infra saga events.

## Transactions & Idempotency

- Append: one transaction per append batch. Consume: one transaction per saga per small batch. Delta apply: one tiny transaction per delta (or tiny batch).
- Idempotency: exactly‑once per saga via `saga_applied`; deltas must be idempotent as well.

## Testing (Given/When/Then)

- given.events: initial events to append.
- when.events: events to append after given (e.g., queries/commands).
- then.emitted: expected events (subset match) after processing (including `db.delta/*` and `query.response`).
- Black‑box DB checks: issue `query.*` events in `when` and assert on `query.response` events in `then`.

Notes:
- We will not implement replay/backfill initially.
- Queries can use joins and arbitrary SQL; deltas remain simple and deterministic.

## Queue & Fairness (Simplicity First)

- Single‑loop dispatcher (JS‑style): one event loop runs all sagas round‑robin.
- Each saga processes up to `N` events, yields; the delta saga applies deltas between rounds.
- Backpressure via queue depth; later we can shard or add per‑saga workers with leases if needed.

## Minimal Developer APIs

- Command saga
```python
# protocols/example/sagas/command_message_create.py
SAGA_NAME = "message.command.create"
SUBSCRIBE = {"types": ["command.message/create"]}

def process(events, db):
    out = []
    for ev in events:
        out.append({
            "id": new_id(),
            "type": "message.created",
            "data": {"sender": ev["data"]["sender"], "text": ev["data"]["text"], "ts": now_ms()}
        })
    return out
```

- Transform/validate saga
```python
# protocols/example/sagas/validate_message.py
SAGA_NAME = "message.validate"
SUBSCRIBE = {"types": ["incoming.decrypted"]}

def process(events, db):
    out = []
    for ev in events:
        if is_valid(ev, db):
            out.append({"id": new_id(), "type": "message.validated", "data": ev["data"]})
        else:
            out.append({"id": new_id(), "type": "validation.error", "data": {"reason": "..."}})
    return out
```

- Domain saga (projector role) → emit deltas
```python
# protocols/example/sagas/message_project.py
SAGA_NAME = "message.project"
SUBSCRIBE = {"types": ["message.validated"]}

def process(events, db):
    out = []
    for ev in events:
        out.append({
          "id": new_id(),
          "type": "db.delta/insert",
          "data": {"table": "messages", "data": {"event_id": ev["id"], "text": ev["data"]["text"], "sender": ev["data"]["sender"]}}
        })
    return out
```

- Query saga
```python
# protocols/example/sagas/query_messages_list.py
SAGA_NAME = "query.messages.list"
SUBSCRIBE = {"types": ["query.messages/list"]}

def process(events, db):
    out = []
    for ev in events:
        rows = db.query("SELECT text, sender FROM messages ORDER BY rowid")
        out.append({"id": new_id(), "type": "query.response", "reply_to": ev["id"], "data": {"rows": rows}})
    return out
```

## Decisions & Open Items

Decisions:
- Infra modeling: everything is events (incoming/outgoing as events; no special tables).
- Delta applier: one global delta saga for now; can shard later if needed.
- Exactly‑once: enforce per saga via `saga_applied`; deltas must be idempotent.
- API: start with ack‑only commands; add convenience endpoints later.
- Protocol boundaries: one protocol at a time; no cross‑protocol sagas.
- Ordering: design domain events to be commutative; results should be invariant to reordering when queries are applied at the end.
- Replay: not required initially.

Open items:
- Validated event lifecycles: diagram lifecycles for framework‑tests, message‑via‑tor, signed‑groups to confirm validators and flows.
- Delta vocabulary: keep `insert|update|delete` for now; joins live on the query side. Reassess need for `upsert`/counters once use cases appear.
- Queue & fairness: implement single‑loop dispatcher with round‑robin batching and yielding; consider per‑saga workers/leases later if needed.

**Questions:**

- how do we best organize events and sagas to avoid a flat explosion of tons of types?
- what is the list of necessary events for the full quiet protocol design? 
 