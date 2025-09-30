Simplifying: Flows As The API
==============================

Objective
- Replace legacy commands with flows (flows are the new “commands”).
- Provide a core helper so flows emit events with one call and return `{ids, data}`.
- Remove placeholder semantics; emit sequentially and pass real IDs between steps.
- Move jobs and reflectors to protocol level and run flows directly.

Guiding Principles
- Flows orchestrate emit + query; they never write to the DB directly.
- Event plaintext contains no self-generated IDs or `signature` fields; the pipeline derives IDs and signs.
- Local-only events (e.g., identity) use the same emit path but are never sent over the network.

Core Additions
- Helper: `FlowCtx.emit_event(event_type, data, by=None, deps=None, network_id=None, local_only=False, seal_to=None, encrypt_to=None, self_created=True, is_outgoing=False) -> str`
  - Builds a canonical envelope with `event_plaintext = {'type': event_type, **data}`.
  - Sets `event_type`, `self_created`, and creator context (`peer_id` via `by`).
  - Normalizes `deps` to `['type:id', ...]`; always includes a `deps` array (empty means no deps).
  - Emits via the pipeline and returns the stored event ID for the emitted type.
- Helper: `FlowCtx.query(query_id, params)` for read-only lookups via the query registry.

Flows = API Operations
- Register with `@flow_op()` in `events/<event>/flows.py`.
- Use `FlowCtx.from_params(params)` to access `_db`, `_runner`, `_protocol_dir`, `_request_id`.
- Return a standard shape: `{ 'ids': {'<type>': id, ...}, 'data': {...} }`.

Canonical Envelope Rules (enforced by handlers)
- No self-generated ID fields in plaintext (IDs are derived; projectors map event to row IDs).
- No `signature` in plaintext (signature handler adds/verifies it).
- Include only necessary business data + creation timestamp.
- Dependencies are explicit as `['type:id', ...]` and validated by resolve_deps.

Placeholder Removal
- Placeholders like `@generated:<type>:<n>` are not used.
- Emit sequentially and pass real IDs from `emit_event` results.
- Pipeline runs a single-pass process; blocked events are unblocked by dependency satisfaction, not placeholders.

Discovery and Exposure
- Flows are discovered by importing `events/<event>/flows.py`.
- Protocol-level `protocols/<name>/api.py` exposes allowed operations: `'flow'` and `'query'`.
- Scheduler loads `protocols/<name>/jobs.py` and executes listed ops (flows) directly.
- Reflect handler consults `protocols/<name>/reflectors.py` to map event types to reflector functions.

Examples
- Simple create (single event)
  - `events/message/flows.py`
  - `@flow_op()`
  - `def create(params):`
    - `ctx = FlowCtx.from_params(params)`
    - `msg_id = ctx.emit_event('message', {'channel_id': ch, 'group_id': '', 'network_id': '', 'peer_id': p, 'content': content, 'created_at': now_ms()}, by=p, deps=[f'channel:{ch}', f'peer:{p}'])`
    - `return { 'ids': {'message': msg_id}, 'data': {...} }`

- Multi-step flow (orchestration)
  - `events/identity/flows.py::create_as_user`
  - Emits identity (local-only) → peer → network → group → user → channel sequentially; returns all IDs.

Migration Checklist
1) Add `FlowCtx.emit_event` and update flows to use it.
2) Convert create operations to `@flow_op()` functions with natural names (e.g., `message.create`).
3) Delete command modules/registry usage; remove placeholder logic.
4) Add protocol-level `api.py` (EXPOSED), `jobs.py`, and `reflectors.py`.
5) Update tests and docs to call flows via API.

Summary
- Flows are the public API surface.
- A small core helper makes emitting events simple and consistent.
- Jobs and reflectors now live at protocol-level and run flows directly.
