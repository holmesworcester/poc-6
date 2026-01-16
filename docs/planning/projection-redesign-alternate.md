# Projection and Command Pipeline: Unified Spec

## Goals
- Simplicity: fewer phases, fewer special cases, minimal spec surface.
- Clean code: clear separation of decode, dependency gating, projection, apply.
- Performance: batch-friendly without mandatory batching.
- Testability: projectors are pure and run without a DB.
- Reasoning-ease: explicit invariants, consistent dependency handling.

## Background (Reality Check)
- Master uses recorded.project() + check_deps() + per-projector DB reads.
- The pure-functional branch adds ProjectorResult and pure projectors, but
  duplicates blob unwrap/parse and splits logic across modules.

This proposal keeps the good pieces but makes the pipeline consistent and
explicit with a single spec and a single projection engine.

## Simplifying Decisions (Locked)
- Single batch engine for all projection (batch of one for commands).
- Tombstone-only deletion (no dependency edge to the message).
- signer_type is embedded in event_data (no polymorphic signer inference).
- Projection resolver uses only table/value/context sources (no helpers, no event_blob).
- Resolver always attaches ctx.signer based on signature verification.

## EventSpec (Required)
Every event type must declare EVENT_SPEC. No defaults.

Example:
```python
EVENT_SPEC = {
    "encrypted": True,
    "signer": {"id_field": "signed_by", "type_field": "signer_type"},
    "requires": {
        "channel": {
            "source": "table",
            "key": "channel_id",
            "table": "channels",
            "fields": ["group_id", "disappearing_time_ms"],
        },
        "author": {
            "source": "table",
            "key": "author_id",
            "table": "users",
            "fields": ["user_id"],
        },
    },
    "optional": {
        "admin_grant": {
            "source": "table",
            "key": "admin_grant",
            "table": "admins",
            "fields": ["user_id"],
        },
    },
    "cascade_on_delete": ["channel"],
}
```

Interpretation
- requires: blocking deps passed to the projector via ctx.deps.
- optional: non-blocking deps passed to the projector via ctx.deps.
- cascade_on_delete: which required deps produce event_dependencies edges.

Required fields
- EVENT_SPEC is mandatory for all event types (shareable and local).
- requires is mandatory (explicit blocking deps, can be empty dict).
- optional is mandatory (explicit optional deps, can be empty dict).
- cascade_on_delete is mandatory (can be empty list).
- encrypted defaults to True if omitted.

### DepSpec (constrained resolver)
Each dep spec has a fixed source type:
- source: table | value | context
- key: field name in event_data used as lookup key (table/value)
- table, fields: required for source=table
- key_from: "@recorded_by" for context source
- context is for lookups keyed by projection scope (for example, networks for recorded_by).

No free-form SQL or procedural logic in specs.

Avoid event_blob (preferred)
- Do not use event_blob in projection specs.
- Required projection data must be present in projection tables.
- If a value is needed for auth or validation, add it to the dependency's
  projection table or add an explicit dependency that guarantees it projects first.

Why explicit specs are still necessary
- You must decide which fields are deps, which are optional, and which are
  local-only (never block).
- You must decide signer id/type fields for signature verification.
- A spec makes those rules explicit and batch-friendly.

## ProjectionContext (Read-only)
Projectors receive a small immutable context with all data resolved up front.

```python
@dataclass(frozen=True)
class ProjectionContext:
    event_id: str
    event_data: dict
    recorded_by: str
    recorded_at: int
    deps: dict
    signer: dict | None  # resolver-populated signer info
```

Projector signature:
```python
def project(ctx: ProjectionContext) -> ProjectorResult:
    ...
```

## Unified Resolver (Decode + Gate + Context)
Single resolver for all event types:

resolve(event_id, recorded_by, recorded_at, db) -> ResolveResult
ResolveResult(success, ctx | failure_reason)

Steps
- Decode once: unwrap + parse + signature verify.
- Extract dep IDs from EVENT_SPEC.requires/optional.
- Check valid_events for required deps (block if missing).
- Fetch dep data using DepSpec (table/value/context).
- Build ProjectionContext and return it.

Signature gating (hard invariant)
- Resolver returns ctx only after signature verification succeeds.
- Invalid signature is reject-only; projectors never see it.
- Resolver attaches ctx.signer for authorization checks.

Dep failure behavior
- Required dep missing from valid_events: block.
- Required dep fetch fails but dep is valid: reject as invariant violation.
- Optional dep fetch fails: pass None to projector.

## Dependency Recording
When projection succeeds, record dependencies based on cascade_on_delete:
INSERT child_event_id -> parent_event_id into event_dependencies.

## Deletion Semantics (Tombstone-only)
- message_deletion does NOT depend on message_id.
- It can project first and record in message_deletions.
- deleted_events is only set after validation (message exists).
- message.project() validates deletion: if valid, insert deleted_events and
  skip projection; if invalid, remove deletion and project.
- recorded.project() should not skip messages based solely on deleted_events
  unless the deletion has already been validated.
- No event_dependencies edge for deletion -> message.

Batch no-flash rule
- Before projecting a batch, build pending_deletions from deletion events.
- When projecting a message, validate against message_deletions plus any
  pending_deletion in the same batch.
- If valid, insert deleted_events and skip message projection.

## Single Batch Engine
- Always run the same projection engine.
- Single-event projection is just a batch of one.
- Ready-queue (Kahn) scheduling is used for intra-batch deps.

Batch flow
1) Fetch and decode all event blobs in the batch.
2) Build dep graph from EVENT_SPEC.requires.
3) Seed ready with events whose required deps are in batch_valid.
4) Project ready events, add to batch_valid, enqueue dependents.
5) Apply results in a single transaction.

## Performance and Simplicity
Performance model (rough)
- Blob-based deps: O(E*D) store reads + O(E*D) decrypts + O(E*D) JSON parses.
- Table-based deps: O(#tables) queries with IN lists + row materialization.
- Crypto unwrap dominates CPU for deps; removing it is a large win.

Simplicity benefits
- Projectors read projection tables today; resolver keeps that model.
- No store fallbacks inside projectors.
- Clear invariants: valid dep implies row exists.

## Command Execution
Commands may create events in two modes. Both use the same projection engine.

### Pure (preferred)
- resolve_command_inputs(spec, ctx) reads tables/context and returns a dict.
- create_pure(inputs, args) returns a CreatePlan (multiple blobs allowed).
- Engine stores blobs, creates recorded entries, then calls project_batch().
- Returns primary_id (or list of ids for multi-event commands).

CreatePlan shape:
```python
CreatePlan = {
    "blobs": [ {"event_id": "...", "event_type": "...", "blob": b"..."} ],
    "primary_id": "..."
}
```

Chain creation
- CreatePlan can include multiple blobs in order.
- Parent events can reference child event_id because ids are content-addressed.
- Use CreatePlan for group_key + group or file_slice + attachment flows.

### Imperative (escape hatch)
- Commands may run queries and call store.event() directly when needed.
- Use this for dynamic workflows (pick_key, lamport counters, key rotation).
- After storing, call project_batch([recorded_id, ...]) and return ids.

This allows complex chains without forcing them into a pure interface while
keeping projection semantics consistent.

## Helper Removal Plan (Projection)
To remove helpers from projection specs, ensure admin_grant is present on all
admin-gated events so authorization can be table-only.

Events that need admin_grant added:
- message_deletion
- message_reaction_deletion
- channel_update
- user_removed
- peer_removed

## Testing Strategy
- Projectors unit-tested with ProjectionContext and dict deps (no DB).
- Resolver tests ensure:
  - invalid signature => reject
  - missing required dep => block
  - optional dep missing => continue
  - required dep fetch failure => reject if dep valid

## Migration Plan (Incremental)
1) Add EVENT_SPEC to all event modules.
2) Embed signer_type in event_data for new events (legacy handled in migration).
3) Introduce unified resolver + batch engine.
4) Convert projectors to pure style incrementally.
5) Remove legacy projection paths once coverage is complete.
