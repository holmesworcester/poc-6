# Forward Secrecy Architecture Plan

## Problem Statement

When messages are deleted, their encryption keys should be purged to prevent recovery. Current implementation has architectural issues:

1. **Blob conflicts on multi-peer devices**: `message_rekey.project()` updates blobs in the shared `store` table, corrupting other peers' views
2. **Missing reference counting**: `message_deletion.project()` deletes blobs without checking if other peers still need them
3. **Blocked event dependencies**: Rekeying can break dependency resolution when events are blocked waiting for missing deps

## Solution: Event/Blob Indirection Layer

Separate **event identity** (stable, referenced by dependencies) from **blob storage** (can change via rekeying):

```
Event ID (stable) → Indirection Layer (per-peer) → Blob ID → Store
```

### Architecture Benefits

✅ **Generic**: Works for any event type (messages, files, etc.)
✅ **Principled**: Clean separation of identity vs storage
✅ **Safe**: Ref counting prevents multi-peer corruption
✅ **SafeDB-only**: All peer-scoped operations use SafeDB
✅ **Dependency-safe**: Blocked events still work (deps reference event_id, not blob_id)
✅ **Convergent**: Deterministic nonces ensure peers reach same new blob_id

## Implementation Plan

### 1. Generic Rekeyed Events Table

**File**: `events/rekeyed_events.sql` (new)

```sql
-- Generic indirection table for events that have been rekeyed
CREATE TABLE IF NOT EXISTS rekeyed_events (
    event_id TEXT NOT NULL,
    current_blob_id TEXT NOT NULL,
    rekeyed_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (event_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_rekeyed_events_lookup
    ON rekeyed_events(event_id, recorded_by);
```

**Update `db.py`**: Add `'rekeyed_events'` to `SUBJECTIVE_TABLES`

### 2. Indirection Module

**File**: `indirection.py` (new)

Provides transparent blob lookup that checks for rekeys:

```python
def get_current_blob_id(event_id: str, recorded_by: str, db: Any) -> str:
    """Get current blob_id for event, checking for rekeys."""

def get_event_blob(event_id: str, recorded_by: str, db: Any) -> bytes:
    """Get blob for event, transparently handling rekeys."""
```

### 3. Reference-Counted Blob Deletion

**Update `store.py`**:

```python
def count_recorded_references(blob_id: str, unsafedb: UnsafeDB) -> int:
    """Count how many recorded events reference this blob_id."""
    # Query store for all recorded events, parse JSON, count ref_id matches

def delete_blob_with_refcount(blob_id: str, recorded_by: str, db: Any) -> bool:
    """Delete blob only if no other peers reference it."""
    # 1. Remove this peer's recorded event for the blob (if any)
    # 2. Count remaining references
    # 3. Delete blob if count == 0
```

### 4. Update Blob Read Paths

Replace direct `store.get(event_id)` calls with `indirection.get_event_blob()`:

- `events/transit/recorded.py` (line ~238)
- `events/content/message_deletion.py` (line ~217)
- `events/content/message_rekey.py`
- Other direct store.get() calls

### 5. Redesign Message Rekeying

**File**: `events/content/message_rekey.py`

Instead of UPDATE blob in store:

1. Read original blob using event_id
2. Decrypt, re-encrypt with new key (deterministic nonce)
3. Create NEW blob with rekeyed ciphertext → new_blob_id
4. Insert into `rekeyed_events`: `(message_id, new_blob_id, recorded_by)`
5. Delete old blob using ref counting

### 6. Fix Message Deletion

**File**: `events/content/message_deletion.py`

Replace direct `DELETE FROM store` with ref-counted deletion:

```python
# Before (line 256-261):
unsafedb.execute("DELETE FROM store WHERE id = ?", (message_id,))

# After:
store.delete_blob_with_refcount(message_id, recorded_by, db)
```

### 7. Clean Up

**Remove**: `events/content/message_rekeys.sql` (replaced by `rekeyed_events.sql`)

## Current Test Failures

### Schema Violation (1 test)
- `test_subjective_tables_have_recorded_by_in_pk`: Table `message_rekeys` missing `recorded_by` in PRIMARY KEY
  - **Fix**: Need to fix message_rekeys table schema to have `recorded_by` in PRIMARY KEY
  - Or: Replace with new `rekeyed_events` table design

### Multi-Peer Synchronization (3 tests)
- `test_admin_group_workflow`: Charlie sees only 1 admin instead of 2 (Alice + Bob)
- `test_file_attachment.py`: File sync issues
- `test_file_attachment_sync_only.py`: File slices not syncing
- `test_file_slice_sync_debug.py`: File slices not syncing

**Root cause**: Likely pre-existing issues unrelated to forward secrecy, but need investigation

### Forward Secrecy Tests
- ✅ All 3 forward secrecy tests PASS
- Tests pass but implementation has the architectural issues identified above

## Next Steps

1. Document the indirection architecture ✓
2. Fix `message_rekeys.sql` schema to add `recorded_by` to PRIMARY KEY
3. Implement rekeyed_events table
4. Add indirection module
5. Add ref-counted blob deletion
6. Update message_rekey.py to use new architecture
7. Fix message_deletion.py to use ref counting
8. Investigate and fix multi-peer sync failures
9. Run full test suite to verify
