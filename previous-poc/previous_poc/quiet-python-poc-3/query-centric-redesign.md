# Query‑Centric Framework Redesign

This redesign reframes the framework around pure functions and a single read abstraction so protocols can be rebuilt and tested deterministically. It follows the approach from the diary:
- Projectors consume events and query results, return deltas
- Commands consume params and query results, return events
- Queries consume params and dict or db, return query results (only queries touch the DB)

The framework orchestrates query execution and delta application; protocol code stays pure and DB‑agnostic.

## Goals

- Pure protocol units: commands, projectors, and queries are deterministic functions with explicit inputs/outputs.
- One read path: queries can run against an in‑memory dict or a real SQLite DB via a common backend.
- DB isolation: only the query layer and the generic delta applier interact with SQLite; protocol code never issues SQL.
- Round‑trip checks: every query test validates dict↔DB equivalence to prevent divergence.
- Replay/idempotency friendly: events and deltas are stable and re‑appliable.

## Core Types

- Envelope: `{ data: {...}, metadata: {...} }` with `data.type` and stable `event_id`.
- Event: `Envelope[DomainEvent]` created by commands or emitted by infra.
- Delta: minimal, declarative mutations applied by the framework:
  - `insert`, `upsert` (via `on_conflict`), `update`, `delete`.
- QueryResult: JSON‑serializable result with deterministic ordering.
- Readset: `Dict[str, Any]` keyed by query IDs used by a command/projector.

## Purity Contracts (per unit)

- Query: `query(params, backend) -> QueryResult`
  - Backend is either `DictBackend(state_dict)` or `SqlBackend(conn)`; both implement the same read primitives.
  - Must not mutate anything.

- Command: `execute(params, reads) -> list[Envelope]`
  - `reads` contains precomputed query results (no DB access), produced by the runtime.
  - Must be deterministic given `params` and `reads`.

- Projector: `project(event, reads) -> list[Delta]`
  - `reads` contains precomputed query results (no DB access).
  - Returns deltas only; projector itself never touches DB.

Framework responsibilities: compute required `reads`, persist events, route to projectors, apply deltas atomically.

## Query Backend Abstraction

Define a minimal “read API” both backends implement so queries can be written once:

```python
class QueryBackend(Protocol):
    def scan(self, table: str) -> Iterable[dict]: ...             # Full scan
    def get(self, table: str, where: dict) -> Iterable[dict]: ... # Equality filters (AND)
    def first(self, table: str, where: dict) -> Optional[dict]: ...
    def order(self, rows: Iterable[dict], by: list[tuple[str, str]]) -> list[dict]: ... # [('col','asc'|'desc')]
    def limit(self, rows: Iterable[dict], n: int) -> list[dict]: ...
```

- `DictBackend`: operates on a canonical dict snapshot of tables.
- `SqlBackend`: translates `get` to parameterized SQL, uses `ORDER BY`, etc.

Queries must produce canonical ordering to ensure deterministic equality across backends.

## Query Definitions

Each query module exports a pure function and metadata:

```python
# protocols/<proto>/queries/messages_in_channel.py
ID = "messages.in_channel"
PARAMS = {"channel_id": "str", "limit": "int?"}

def run(params: dict, backend: QueryBackend) -> dict:
    rows = backend.get("messages", {"channel_id": params["channel_id"]})
    rows = backend.order(rows, [("timestamp", "desc"), ("id", "desc")])
    if params.get("limit"):
        rows = backend.limit(rows, int(params["limit"]))
    # Return stable shape
    return {"messages": rows}
```

Notes:
- Implement in terms of `get/scan/order/limit` only; no backend‑specific assumptions.
- Keep shapes small and typed (via JSON Schema files if available) so they’re easy to assert on.

## Declaring Read Dependencies

Commands/projectors declare the queries they need, plus how to derive query params:

```python
# protocols/<proto>/commands/message_create.py
REQUIRES = [
  {"id": "identity.current", "params": lambda p: {}},
  {"id": "channel.by_name", "params": lambda p: {"name": p["channel"]}},
]

def execute(params: dict, reads: dict) -> list[Envelope]:
    me = reads["identity.current"]["identity"]
    chan = reads["channel.by_name"]["channel"]
    evt = {
      "type": "message.created",
      "sender": me["pubkey"],
      "channel_id": chan["id"],
      "text": params["text"],
      "timestamp": params["now_ms"],
    }
    return [{"data": evt, "metadata": {}}]
```

```python
# protocols/<proto>/projectors/messages.py
REQUIRES = [
  {"id": "peers.known_by", "params": lambda e: {"pubkey": e["sender"], "received_by": e["channel_owner"]}},
]

SUBSCRIBE = ["message.validated", "message.created"]

def project(event: dict, reads: dict) -> list[Delta]:
    is_unknown = len(reads["peers.known_by"]["peers"]) == 0
    return [
      {
        "op": "upsert",
        "table": "messages",
        "data": {
          "event_id": event["event_id"],
          "text": event["text"],
          "sender": event["sender"],
          "channel_id": event["channel_id"],
          "timestamp": event["timestamp"],
          "unknown_peer": is_unknown,
          "created_at": event["timestamp"],
        },
        "on_conflict": {"key": ["event_id"], "action": "ignore"}
      }
    ]
```

The framework evaluates `REQUIRES` by running the referenced queries with params derived from the command params or the event.

## Runtime Orchestration

- Run Command
  1) Build readset: for each `REQUIRES`, run query with `SqlBackend(db)`.
  2) Call `execute(params, reads)` to get events.
  3) Persist events into the event store (idempotent on `event_id`).
  4) Dispatch events to projectors.

- Run Projector
  1) For each subscribed event, build readset via queries with `SqlBackend(db)`.
  2) Call `project(event, reads)` to get deltas.
  3) Apply deltas atomically using the generic applier (no protocol SQL inside projector).
  4) Record `(projector,event_id)` in an applied ledger for idempotency.

- Tick/Jobs: unchanged conceptually; ticks trigger background commands (queue drainers) that are also pure commands consuming query reads and emitting events.

## Delta DSL and Appliers

Keep the DSL minimal and generic so tests can run fully in‑memory and in SQL:

```json
{
  "op": "upsert|insert|update|delete",
  "table": "messages",
  "data": {"col": value},
  "where": {"col": value},
  "on_conflict": {"key": ["event_id"], "action": "ignore|update"}
}
```

Appliers:
- DictApplier: applies to a dict of tables (lists of rows). Enforces unique keys and stable ordering by primary key.
- SqlApplier: translates DSL to parameterized SQL and runs inside a transaction. This module and the query backends are the only DB‑touching code paths.

## Dict↔DB Equivalence For Queries

Every query test validates that dict and DB sources yield identical results, with a round‑trip sanity check.

Test pattern:

1) Given initial dict state `S` (canonical tables as lists of rows), and query `Q` with `params P`.
2) Compute `R_dict = Q.run(P, DictBackend(S))`.
3) Seed a temporary SQLite DB from `S` (generic seeder uses protocol `schema.sql`).
4) Compute `R_sql = Q.run(P, SqlBackend(conn))`.
5) Assert `R_dict == R_sql` (deep equality, stable ordering guaranteed by the query).
6) Round‑trip check: dump DB to dict `S'` (generic dumper), then assert `Q.run(P, DictBackend(S')) == R_sql`.

Notes:
- Seeder/dumper already exist in the framework in rough form; formalize them as `seed_db_from_dict(schema, S)` and `dump_db_to_dict(conn)`.
- Normalize booleans, JSON columns, and ordering to canonical forms before comparison.
- The round‑trip step prevents accidental backend‑only behavior in queries.

## Testing Strategy (unit → integration)

- Query unit tests: pure; define `S` and `P`, assert dict/sql parity and round‑trip.
- Command unit tests: build `reads` by invoking queries with `DictBackend(S)`; call `execute` and assert events.
- Projector unit tests: build `reads` by invoking queries with `DictBackend(S)`; call `project` and assert deltas; optionally apply deltas to dict and assert table shapes.
- Integration tests (runner): use `SqlBackend` and `SqlApplier`; run command → stored events → projector application → assert tables; compare to expected tables or to a “dict simulation” run for the same steps.

## Repository Layout

- `core/`
  - `query_backend.py`: `QueryBackend`, `DictBackend`, `SqlBackend`.
  - `query_runtime.py`: dependency resolution and readset construction; helpers to run queries by ID.
  - `delta.py`: Delta DSL types; `DictApplier` and `SqlApplier`.
  - `event_store.py`: append/read events; idempotency keys.
  - `command_runtime.py`: run command (compute reads, emit events, persist, dispatch).
  - `projector_runtime.py`: run projector (compute reads, apply deltas, ledger).
  - `seed_dump.py`: `seed_db_from_dict`, `dump_db_to_dict` (uses schema.sql and generic rules).
  - `test_runner.py`: add query parity hooks to query tests; stable ordering helpers.

- `protocols/<proto>/`
  - `queries/*.py`: pure query functions.
  - `commands/*.py`: pure commands, declare `REQUIRES`.
  - `projectors/*.py`: pure projectors, declare `REQUIRES` and `SUBSCRIBE`.
  - `schema.sql`, `api.yaml`, existing `demo/`.

## Example Flow (message create → project)

- Command `message.create(params)` requires `identity.current` and `channel.by_name`; returns `message.created` event.
- Projector `messages.project(event)` requires `peers.known_by`; returns an `upsert` delta to `messages` with `unknown_peer` flag set accordingly.
- Runtime computes reads via `SqlBackend`, persists event, applies deltas atomically.
- In unit tests, the same functions run entirely on dict state via `DictBackend` with no SQLite.

## Migration Plan

1) Introduce `QueryBackend`, `DictBackend`, `SqlBackend`, and wire a thin `run_query(id, params, backend)`.
2) Extract a first set of queries from current handlers (e.g., `messages.in_channel`, `identity.current`).
3) Wrap current projector code to return deltas instead of issuing SQL directly.
4) Add `DictApplier` and `SqlApplier` with minimal `insert|upsert|update|delete` support.
5) Update the test runner to:
   - Recognize query tests and perform dict↔DB parity + round‑trip.
   - Allow command/projector unit tests to supply readsets computed via `DictBackend`.
6) Migrate one protocol (e.g., `message_via_tor`) as a reference implementation.

## Open Questions For You

- Query scope: Are `scan/get/order/limit` primitives sufficient, or do you want joins/group‑by in the query API? If so, should we add a `join(left, right, on)` helper implemented per backend?
- Result shape: Do you prefer queries to return arrays at the top level (e.g., `{ items: [...] }`) or typed objects with named fields (as sketched above)? Any schema you want enforced by the runner?
- Delta semantics: Is `on_conflict: { key: [..], action: ignore|update }` enough, or should we support partial updates on conflict (e.g., `set` subset)?
- Event identity: Confirm the source of `event_id` (hash of canonical data) so idempotency can be enforced in event store and projector ledgers.
- Time/entropy injection: Should `now_ms` and random IDs be runtime‑injected (passed in `params`) rather than read from the system clock inside commands?
- Canonical ordering: For query equality across backends, confirm default tie‑breakers (e.g., `ORDER BY timestamp DESC, id DESC`). Any global conventions you want?
- Seeder/dumper contract: Is it acceptable to base this on `schema.sql` only (like the current generic seeder/dumper), or do you want per‑protocol hints for JSON/boolean columns?

## Appendix: Minimal Interfaces (sketch)

```python
# core/query_runtime.py
REGISTRY = { ID: run_fn, ... }

def run_query(id: str, params: dict, backend: QueryBackend) -> dict:
    return REGISTRY[id](params, backend)

# core/command_runtime.py
def run_command(cmd, params, db):
    reads = {q['id']: run_query(q['id'], q['params'](params), SqlBackend(db.conn)) for q in cmd.REQUIRES}
    events = cmd.execute(params, reads)
    # persist + dispatch...
    return events

# core/projector_runtime.py
def run_projector(proj, event, db):
    reads = {q['id']: run_query(q['id'], q['params'](event), SqlBackend(db.conn)) for q in proj.REQUIRES}
    deltas = proj.project(event['data'], reads)
    SqlApplier(db.conn).apply(deltas)
```

If this matches what you had in mind, I can draft the core stubs (`QueryBackend`, `DictBackend`, `SqlBackend`, `DictApplier`, `SqlApplier`), and adapt one or two queries/projectors from `message_via_tor` to prove the loop, along with the query parity checks in `core/test_runner.py`.


## Simplifications Adopted (Agreed)

- Scope
  - Single-phase flow: Execute command then project synchronously in one transaction. Event log/dispatcher is optional.
  - Idempotency via upserts: Rely on unique constraints and idempotent upserts; add projector ledgers later only if needed.
  - No background tick initially: Model drainers/background work as explicit commands; add ticking later.

- Dependency wiring
  - Inject a read-only query facade `q(id, **params)` bound to a consistent snapshot. Prefer `execute(params, q)` and `project(event, q)` over predeclared `REQUIRES` lists.
  - Support simple name-based param mapping from `params`/`event`; allow custom mapping when names differ.

- Projector/command API
  - Convention over config: Route events to projector functions by name (e.g., `project_message_created`).
  - Commands return only envelopes (`[{ data, metadata }]`). Fresh state responses come from queries.

- Schema/types
  - Event identity: `event_id = hash(canonical_json(data))` for idempotency.
  - Determinism: Inject `now_ms` and ID generators via params; do not read clocks or RNGs inside pure units.

- Repository/runtimes
  - Keep current `core.command.run_command` and `core.handle`; add thin shims to inject `q` and apply upsert deltas.
  - Single query registry for lookup; avoid new runtime layers unless async/replay is introduced later.

## API Queries and Preserving DB Power

For API endpoints that need fully hydrated views (e.g., message lists with edits, reactions, unfurls, attachments):

- Two tiers of queries
  - Core queries: portable across dict and SQL backends for purity and unit testing; used by commands/projectors.
  - API queries: SQL-native and free to use joins, CTEs, window functions, and JSON1 to hydrate efficiently. These may be SQLite-only (or target your chosen RDBMS) and are tested against the DB backend.

- Structure
  - Place API queries under `protocols/<proto>/queries_api/` and expose them to the API layer. Keep core queries under `protocols/<proto>/queries/`.
  - API routes call API queries directly; command/projector flows stick to core queries and deltas.

- Testing
  - API queries: seed DB using existing generic seeder; assert deterministic results with explicit ordering.
  - Core queries: keep dict↔DB parity and round-trip checks.

## Minimal Runtime Variant (Default)

- Build a `q` facade over `SqlBackend(db)` per transaction (consistent snapshot for the operation).
- Run `execute(params, q)` → envelopes.
- For each envelope, route to projector function(s) by naming and run `project(event, q)` → deltas.
- Apply deltas via a minimal SQL upsert applier (and a tiny dict applier for unit tests where useful).
- Optionally append envelopes to an event log for debugging/auditing.

Advanced modules (optional later): add event store, async dispatcher, and projector ledgers when the product needs replay/backfill or multi-consumer fan-out.
