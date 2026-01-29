# Feedback: v2-conversions Branch

Reviewed commits `db32154..ebaafa0` (13 commits) for projection v2 migration.

---

## Overall Assessment

Strong work. The architecture is clean, dual-dispatch is correctly wired, and the incremental approach is paying off. 14 projectors are now running through v2 in production with full parity testing. A few things to tighten up before continuing.

---

## What's Working Well

### 1. Dual-Dispatch is Live
The `recorded.py` dispatch correctly routes to v2 when both `EVENT_SPEC` and `project_pure` exist:
```python
if event_spec and project_pure_fn:
    # v2 path: resolve → project_pure → apply_writes
```
This means all converted projectors are exercised in real flows, not just tests.

### 2. Clean Separation of Concerns
- `types.py`: Frozen dataclasses, no behavior
- `resolver.py`: Dependency resolution + signature verification
- `apply.py`: WriteOp → SQL translation
- Event modules: Pure `project_pure()` functions

### 3. Parity Test Pattern
The test structure is solid:
```python
legacy_db = _new_db()
v2_db = _new_db()
_project_legacy(legacy_db, ...)
_project_v2(v2_db, ...)
assert get_table_rows(legacy_db) == get_table_rows(v2_db)
```
This catches behavioral drift early.

### 4. Consistent v2 Patterns Across Modules
All converted projectors follow the same shape:
- `EVENT_SPEC` with explicit `signer`, `requires`, `optional`
- `project_pure(ctx)` returns `ProjectorResult(writes=(...), valid_event=True/False)`
- `signer_type` field added to `create()` for v2 resolver

---

## Issues to Address

### 1. `transit_prekeys` Table Scoping Mismatch

In `transit_prekey.py`, the table is written to `unsafedb` (global), but in `project_pure()`:
```python
WriteOp(
    op='insert',
    table='transit_prekeys',
    values={...},  # No recorded_by
)
```

Meanwhile `apply.py` assumes subjective tables need `recorded_by`. If `transit_prekeys` isn't in `SUBJECTIVE_TABLES`, this works, but verify this is intentional. The legacy `project()` also uses `unsafedb`, so parity is maintained - just confirm the table scoping is correct.

### 2. `group_prekey.py` Has Subtle Difference

Legacy `project()` marks the event valid:
```python
safedb.execute(
    "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
    (prekey_id, recorded_by)
)
```

But `project_pure()` doesn't - it relies on `recorded.py` to do this after `apply_writes()`. This is correct behavior, but the legacy `project()` shouldn't be marking valid_events since that's recorded.py's job. The duplication is harmless (INSERT OR IGNORE) but confusing.

**Suggestion**: Remove the `valid_events` insert from legacy `project()` in group_prekey.py to match the v2 pattern. Same applies to other legacy projectors that still mark valid_events directly.

### 3. Silent Failures in `project_pure()`

When validation fails, `project_pure()` returns:
```python
return ProjectorResult(writes=tuple(), valid_event=False)
```

No indication of *why* it failed. This makes debugging hard when events silently don't project.

**Suggestion**: Add an optional `reason` field:
```python
@dataclass(frozen=True)
class ProjectorResult:
    writes: tuple[WriteOp, ...]
    valid_event: bool = True
    reason: str | None = None  # Populated when valid_event=False
```

Then in `recorded.py`:
```python
if not projector_result.valid_event:
    log.warning(f"[PROJECTION_FAILED] {event_type} {ref_id[:20]}...: {projector_result.reason}")
```

### 4. `observed_address` vs `self_address` Table Inconsistency

- `observed_address` writes to `network_addresses`
- `self_address` writes to `addresses`

Two different tables for address data. Is this intentional? The legacy code has the same pattern, so parity is correct, but worth confirming the schema design.

### 5. Test Coverage Gaps

Missing parity tests for:
- `transit_prekey_shared` (only `transit_prekey` tested)
- `group_prekey_shared` (only `group_prekey` tested)
- `self_address` (only `observed_address` tested)
- `intro.py` (has EVENT_SPEC + project_pure but no parity test visible)

The `*_shared` variants involve signature verification, which is the more complex path. Worth adding explicit tests.

### 6. `cascade_on_delete` is Unused

Every `EVENT_SPEC` has:
```python
'cascade_on_delete': [],
```

But I don't see any resolver or apply logic that processes cascades. Either:
- Remove the field if it's not needed yet
- Document that it's a placeholder for future work

---

## Minor Observations

### Redundant Legacy Code
Modules like `peer.py` have both `project()` and `project_pure()` doing essentially the same thing. Since v2 is live, the legacy `project()` is only called when someone calls it directly (not through `recorded.project()`). Consider:
- Removing legacy `project()` from fully-converted modules
- Or adding a deprecation comment

### Type Annotations
`project_pure(ctx: Any)` could be `project_pure(ctx: ProjectionContext)` for better IDE support and documentation.

### `_field_looks_like_pubkey` Heuristic
```python
def _field_looks_like_pubkey(field_name: str | None) -> bool:
    lowered = field_name.lower()
    return "pubkey" in lowered or "public_key" in lowered
```
This is fragile. If someone adds a field like `backup_pubkey_reference` that's actually an event ID, this would break. Consider making this explicit in `SignerSpec`:
```python
class SignerSpec(TypedDict, total=False):
    id_field: str
    type_field: str
    id_is_raw_pubkey: bool  # True if id_field contains raw pubkey, not event ID
```

---

## Summary

The v2 migration is on track. Main action items:
1. Add parity tests for `*_shared` variants and `self_address`
2. Consider adding `reason` field to `ProjectorResult` for debugging
3. Clean up legacy `valid_events` inserts that are now redundant
4. Document or remove `cascade_on_delete` placeholder

Good incremental progress - 14 projectors converted and live.
