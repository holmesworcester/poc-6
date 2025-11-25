# Self-Connection Bug in Linked Device Sync

## Problem Description

When two devices (Device 1 and Device 2) link to the same user account in a shared-database test scenario, both devices would attempt to send sync requests to themselves. This occurred because both devices saw the same `sync_connections` table and would iterate over all connections without checking if they were connecting to themselves.

## Root Cause

The `sync_connections` table is device-wide (no `recorded_by` field), meaning all devices in a shared database see the same connections. When Device 2 sends a `sync_connect` to Device 1, an entry is created in `sync_connections`:

```sql
INSERT INTO sync_connections (peer_shared_id, ...) VALUES ('Device2_peer_shared_id', ...);
```

When `sync.send_requests()` runs for each device:
- **Device 1** queries `sync_connections`, finds Device 2, sends sync request to Device 2 ✅
- **Device 2** queries `sync_connections`, finds Device 2 (itself!), sends sync request to itself ❌

## Manifestation

In logs, you would see:

```
[SYNC_SEND] from_peer=Device1... connections=1 ids=['Device2...']
[SYNC_REQUEST] from=Device1... to=Device2...           # Correct

[SYNC_SEND] from_peer=Device2... connections=1 ids=['Device2...']
[SYNC_REQUEST] from=Device2... to=Device2...           # Self-connection!
```

This wastes resources and can cause:
- Unnecessary sync request processing
- Bloom filter checks against the device's own events
- Potential infinite loops or cyclic behavior
- Confusing debug output

## Fix

Added a self-connection filter in `events/network/sync.py:send_requests()`:

```python
for row in connection_rows:
    peer_shared_id = row['peer_shared_id']

    # Skip connections to ourselves (can happen in shared-database tests)
    if peer_shared_id == from_peer_shared_id:
        log.warning(f"[SYNC_SEND] skipping self-connection: from={from_peer_shared_id[:10]}... to={peer_shared_id[:10]}...")
        continue

    # Send sync request to this connected peer
    send_request(peer_shared_id, from_peer_id, from_peer_shared_id, t_ms, db)
```

## Why This Occurs in Shared-Database Tests

In production, each device has its own isolated database. The `sync_connections` table on Device 1 only contains connections **as seen by Device 1**. But in test scenarios using a single in-memory database shared by multiple simulated devices, all devices see the same `sync_connections` table.

This is a test-specific edge case, but the fix is still valuable because:
1. It's defensive programming - prevents unexpected behavior
2. Makes test scenarios more realistic
3. Simplifies debugging by removing confusing self-request logs

## Related Issues

This bug was discovered while diagnosing linked device test failures. It was one of several issues preventing Device 1 from receiving Device 2's `transit_prekey_shared`:

1. ✅ **Self-connection bug** (fixed in this commit)
2. ✅ **Broken bootstrap code** (removed in same commit)
3. ❌ **Sync response delivery** (still under investigation)

## Testing

Before fix:
```
[SYNC_SEND] from_peer=Device2... connections=1 ids=['Device2...']
[SYNC_REQUEST] from=Device2... to=Device2...
```

After fix:
```
[SYNC_SEND] from_peer=Device2... connections=1 ids=['Device2...']
[SYNC_SEND] skipping self-connection: from=Device2... to=Device2...
```

## Commit

Fixed in commit `e1cfb6b`: "Fix linked device sync: remove broken bootstrap and add self-connection filter"
