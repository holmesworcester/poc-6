# Minimal Event Model (id, type, protocol-defined fields)

This document proposes simplifying the framework’s event contract to three fields and giving protocols full freedom over everything else.

Event = { id: string, type: string (handler name), ...protocolFields }

The core framework guarantees routing by `type` and transaction boundaries; protocols own the rest: schema, validation, projection rules, and any extra transport/source details. There is no required `data` field — protocols may structure additional fields however they like.

---

## Spec

- id: string. Protocol-generated, using any scheme it wants (UUID, hash, domain id). Must be present when the event is emitted.
- type: string. Must match a handler directory name (e.g., `message`, `peer`, `link`).
- ...protocolFields: zero or more protocol-defined top-level fields. JSON-serializable.

No required metadata at the framework level.
- If a protocol needs “source”, “received_by”, signatures, or transport hints, include them as top-level fields (or in protocol tables) as you prefer.

---

## Core Responsibilities

- Routing: `handle(db, event, time_now_ms)` dispatches by `event.type` to `<type>/projector.py`.
- Transactions: One transaction per event by default; command-generated events participate in the parent transaction.
- IDs: Must be present. Core validates `event.id` and `event.type` are non-empty; if missing, it errors.
- Time: Pass `time_now_ms` to projectors.
- Validation: Required. Core validates every event against the handler’s JSON schema before projection. The schema is provided by the protocol per type.

Recommended helper (core):
- validate_event(item):
  - Requires `{ id: str, type: str }` at minimum
  - Extracts protocol fields (event minus `id` and `type`) and validates against the handler’s schema
  - Raises a clear error if required fields are missing or invalid

---

## Protocol Freedom

- Projectors read the event’s protocol-defined fields (top-level) and persist to protocol-owned tables.
- If a protocol wants source scoping (identity/user/network), include it as a top-level field or denormalize to columns in its own tables.
- “Unknown” or “missing_key” can be implemented as regular handlers if a protocol needs those concepts.

---

## Event Storage & Deletion

The framework does not own or mandate an event store. Each protocol:
- Designs its own storage (tables/indices) and controls retention and deletion.
- May provide helpers (e.g., `protocols/<name>/event_store.py`) to append/query/delete as needed. (this could also be in the protocol root or utils directory)
- Can choose hard or soft delete semantics without involving core.

---

## Command Path

- Commands return `{ "newEvents": [{ id: str, type: str, ...protocolFields }, ...], ... }`.
- Core validates each event (presence of id/type) and projects within the current transaction.
- `newEnvelopes` is replaced by `newEvents` in this model.

---

## Projector Contract

- Signature: `project(db, event, time_now_ms)`
- Early return if `event.type` doesn’t match your handler (defensive, cheap).
- Read whatever protocol fields you defined (top-level or nested), and project into protocol tables.
- SQL-first projection; idempotency via `event.id` or protocol-level domain ids.

---

## Migration Plan (script-driven)

1) Replace envelope usage:
   - `payload = envelope.get('payload', {})` -> reference event fields directly (protocol-defined)
   - `event_type = payload.get('type')` -> `event_type = event.get('type')`
   - Remove `metadata` references; move necessary fields to top-level fields or protocol tables.

2) Update core:
   - `core/handle.py`: expect `{ id, type, ... }`; remove envelope routing/metadata branches.
   - `core/command.py`: accept only `newEvents`; validate each event (id/type + schema) and project within the transaction.
   - `core/schema_validator.py`: invoked by core to validate protocol fields (event minus `id` and `type`).

3) Event storage:
   - Move persistence fully into protocols; drop any framework-run event store.

4) Tests & API:
   - Update test JSON to use `{ id, type, ... }` everywhere.
   - If an event listing API is needed, expose it per protocol (or via an aggregator that calls protocol stores).

---

## Validation & Testing

- Deterministic tests: continue using per-test DBs via `TEST_DB_PATH` and set `CRYPTO_MODE=dummy` when needed.
- Per-handler JSON schema is required.
  - Location: `protocols/<name>/handlers/<type>/<type>_handler.json` under key `schema`.
  - Scope: describes protocol-defined fields (top-level fields other than `id` and `type`).
  - Core extracts `{...protocolFields}` = event minus `id` and `type` and validates it using the schema.
  - Tests must include schemas for all event types used.

Example minimal schema (protocol-defined):

```
{
  "type": "object",
  "required": ["text", "sender"],
  "properties": {
    "text": { "type": "string", "minLength": 1 },
    "sender": { "type": "string" },
    "received_by": { "type": "string" }
  },
  "additionalProperties": true
}
```

---

## Trade-offs

Pros
- Maximum protocol freedom; minimal core coupling.
- Simplified routing and storage; fewer invariants to maintain.
- Easier to migrate protocols that have diverging needs.

Cons
- Cross-protocol tools lose standardized metadata; protocols must opt-in to any shared conventions.
- Replay/analytics that relied on shared metadata must read protocol-specific shapes.

---

## Next Steps (suggested)

- Add `core/events.py` with `validate_event()` (and small helpers if useful).
- Update `core/handle.py` and `core/command.py` to the minimal model.
- Provide a one-time script to:
  - Rewrite projectors from `envelope.payload`/`metadata` to `event.data`.
  - Update protocol event_store schemas.
  - Update tests and fixtures.

If you want, I can implement `ensure_event()` and adapt `handle.py`/`command.py` to this model next.
