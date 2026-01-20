# Prioritized File Sync (sync_file) - Archived Feature

**Status:** Removed in favor of negentropy-based sync
**Last working commit:** `984dd406252558b30c03dc892ca8d42982e9633e`
**Repository:** https://github.com/holmesworcester/poc-6/tree/984dd406252558b30c03dc892ca8d42982e9633e

## Overview

The `sync_file` system provided **prioritized, on-demand synchronization** of file slices. Unlike regular negentropy sync which syncs all events opportunistically, sync_file allowed users to:

1. **Request specific files** with priority levels
2. **Pause/resume** file downloads
3. **Track progress** of individual file downloads

## Why It Was Removed

- File slices (`file_slice` events) are already `SHAREABLE = True` and sync via negentropy
- The prioritization added complexity without clear benefit in practice
- The bloom filter + windowing approach was a separate protocol from negentropy, creating maintenance burden
- Simpler to let negentropy handle all event sync uniformly

## How It Worked

### Protocol: Bloom Filters + Windowing

Unlike negentropy (which uses set reconciliation), sync_file used a simpler bloom filter approach:

```
Requester                              Responder
    |                                      |
    |  sync_file event:                    |
    |  {file_id, window_id, bloom}         |
    |------------------------------------->|
    |                                      |
    |  Checks bloom for slices             |
    |  requester DOESN'T have              |
    |                                      |
    |         file_slice events            |
    |<-------------------------------------|
```

### Windowing System

Files were divided into windows to keep bloom filter false-positive rates low:

```python
# Window parameter calculation
w_param = compute_file_w_param(blob_bytes)
# w_param = bits for window count (0-12 → 1 to 4096 windows)
# Target: ~100 slices per window

total_windows = 2 ** w_param
window_id = hash(event_id) >> (256 - w_param)
```

For a 1MB file (~2222 slices at 450 bytes/slice):
- `w_param = 5` (32 windows)
- ~70 slices per window

### Bloom Filter

- **Size:** 512 bits (64 bytes)
- **Hash functions:** 5 (K_HASHES)
- **Salt:** Derived from peer's public key + window_id

```python
def derive_salt(peer_pk: bytes, window_id: int) -> bytes:
    """BLAKE2b-128(peer_pk || window_id)"""
    window_id_bytes = window_id.to_bytes(4, byteorder='big')
    return hashlib.blake2b(peer_pk + window_id_bytes, digest_size=16).digest()
```

Synthetic event IDs for bloom membership:
```python
# For each slice we have:
syn_id = crypto.hash(file_id_bytes + slice_number.to_bytes(4, 'big'), 16)
```

### Database Tables

```sql
-- Files user wants to sync (with priority)
CREATE TABLE file_sync_wanted (
    file_id TEXT NOT NULL,
    peer_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    priority INTEGER DEFAULT 5,      -- 1-10, higher = more urgent
    status TEXT DEFAULT 'active',    -- 'active', 'paused'
    ttl_ms INTEGER DEFAULT 0,        -- 0 = forever
    requested_at INTEGER,
    PRIMARY KEY (file_id, peer_id, recorded_by)
);

-- Progress tracking per file/peer pair
CREATE TABLE file_sync_state_ephemeral (
    file_id TEXT NOT NULL,
    from_peer_id TEXT NOT NULL,
    to_peer_id TEXT NOT NULL,
    last_window INTEGER DEFAULT -1,  -- Last window synced
    w_param INTEGER DEFAULT 0,       -- Window bits
    slices_received INTEGER DEFAULT 0,
    total_slices INTEGER DEFAULT 0,
    started_at INTEGER,
    updated_at INTEGER,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (file_id, from_peer_id, to_peer_id, recorded_by)
);
```

### API Functions

```python
# Mark file for active sync
sync_file.request_file_sync(file_id, peer_id, priority=5, ttl_ms=0, t_ms=t_ms, db=db)

# Pause/resume
sync_file.pause_file_sync(file_id, peer_id, db)
sync_file.resume_file_sync(file_id, peer_id, db)

# Cancel
sync_file.cancel_file_sync(file_id, peer_id, db)

# Check completion
is_complete = sync_file.is_file_complete(file_id, peer_id, db)

# Send request to specific peer
sync_file.send_request(file_id, to_peer, from_peer_id, t_ms, db)

# Handle incoming request (called during projection)
sync_file.project(event_id, recorded_by, recorded_at, db)
```

### Integration Points

1. **message_attachment.py** - Called `request_file_sync()` when attachment received
2. **cli.py** - Exposed `is_file_complete()`, pause/resume commands
3. **api/routes/files.py** - HTTP endpoints for file sync control

### Event Format

```json
{
    "type": "sync_file",
    "file_id": "base64-encoded-file-id",
    "requester_peer_id": "peer-id-requesting-slices",
    "window_id": 3,
    "w_param": 5,
    "bloom": "base64-encoded-512-bit-bloom-filter",
    "created_at": 1705000000000,
    "signature": "..."
}
```

## Files to Restore

To resurrect this feature, restore these files from the commit above:

```
events/network/sync_file.py      # Main implementation (560 lines)
events/network/sync_file.sql     # Database schema
tests/scenario_tests/test_large_file_sync.py
tests/scenario_tests/test_download_progress_accuracy.py
tests/scenario_tests/test_file_demo_cli.py
tests/scenario_tests/test_file_pause_resume.py
tests/networking/test_file_sync.py
tests/test_file_consolidation.py
```

And restore the integration calls in:
- `events/content/message_attachment.py` (3 `request_file_sync()` calls)
- `cli.py` (pause/resume commands, `is_file_complete()`)
- `api/routes/files.py`
- `core/db.py` (schema loading)

## Alternative: Simple Progress Check

If you just need `is_file_complete()` without the full sync system:

```python
def is_file_complete(file_id: str, peer_id: str, db) -> bool:
    """Check if all slices for a file have been received."""
    row = db.query_one("""
        SELECT
            (SELECT total_slices FROM message_attachments
             WHERE file_id = ? AND recorded_by = ? LIMIT 1) as total,
            (SELECT COUNT(*) FROM file_slices
             WHERE file_id = ? AND recorded_by = ?) as received
    """, (file_id, peer_id, file_id, peer_id))

    if not row or not row['total']:
        return False
    return row['received'] >= row['total']
```
