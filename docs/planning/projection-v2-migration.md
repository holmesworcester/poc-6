# Projection v2 Migration Plan

This doc defines the shared interface for v2 projection and the incremental
migration steps. It is the coordination reference for parallel work.

## Goals
- Keep legacy projectors in place during migration.
- Add pure projectors incrementally behind a v2 resolver + batch engine.
- Ensure behavior equivalence before cutover.

## V2 API Shape (Frozen)
Module: `core/projection/`

Types (`core/projection/types.py`):
- `ProjectionContext(event_id, event_type, event_data, recorded_by, recorded_at, deps, signer)`
- `ProjectorResult(writes, valid_event=True)`
- `WriteOp(op, table, values, where=None)`
- `ResolveResult(status, ctx, missing=(), error=None)`
- `EventSpec`, `DepSpec`, `SignerSpec` (TypedDict shapes)

DepSpec notes:
- `required_if_present` (bool): when `True`, treat the dependency as optional
  unless the event field is present; if present but missing/invalid, block
  instead of treating it as `None`. This preserves legacy “conditional deps”
  (e.g., `admin_grant`).

Functions (stubs in scaffolding):
- `resolve_event(event_id, recorded_by, recorded_at, db) -> ResolveResult`
- `project_batch(recorded_ids, db) -> list[str | None]`
- `apply_writes(result, recorded_by, recorded_at, db) -> None`

## Event Module Contract (v2)
Each event module will eventually expose:
- `EVENT_SPEC = {requires, optional, cascade_on_delete, signer, encrypted}`
- `project_pure(ctx: ProjectionContext) -> ProjectorResult`
- Legacy `project()` stays until final cutover.

## Registry Hooks (v2)
`events/registry.py` will expose:
- `get_event_spec(event_type)`
- `get_project_pure_fn(event_type)`

This lets `recorded.project()` dispatch to v2 when a module opts in.

## Dual-Dispatch Plan
In `events/network/recorded.py`:
- If event_type has `EVENT_SPEC` and `project_pure`, call v2 resolver + apply.
- Otherwise fall back to legacy `project()` path.
- Block on missing deps using the same queues logic as today.

## Pilot Conversion Set
Convert these first (keep legacy projector in file):
1) `events/identity/network.py`
2) `events/identity/admin.py`
3) `events/identity/username_update.py`
4) `events/identity/network_name_update.py`
5) `events/group/group_key.py`

## Event Conversion Checklist
For each event:
1) Add `EVENT_SPEC` with explicit `requires`, `optional`, `cascade_on_delete`.
2) Implement `project_pure(ctx)` to produce `WriteOp`s.
3) Keep `project()` intact (legacy path).
4) Add tests that compare legacy vs v2 outcomes on the same inputs.

## Testing Guidelines
Add tests under `tests/projection/`:
- Build a minimal DB state.
- Project an event via legacy path and snapshot relevant rows.
- Project the same event via v2 path and compare rows.
- Include block/reject cases for deps and signature verification.

## Final Cutover
Once coverage is complete:
- Remove legacy projector calls in `recorded.project()`.
- Delete legacy `project()` implementations.
- Remove any compatibility shims (e.g., legacy signer inference).
- Update docs to reflect pure-only projector pipeline.

## Open Issues (TODO at End)
- TODO(end): Confirm `INTRO_TTL_MS` for `network_intro`. Behavior: drop intros when
  `recorded_at - created_at` exceeds TTL (no pending intro writes).
