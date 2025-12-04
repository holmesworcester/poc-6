# Plan: Wire Sync to Network Simulator

## Current Problem

Sync bypasses NAT simulation because it adds packets directly to queues without peer information:

```python
# In sync.py line 957:
queues.incoming.add(request_blob, t_ms, db)  # No from_peer/to_peer!
```

The NAT enforcement in `queues/incoming.py` only works when `from_peer` and `to_peer` are provided:

```python
def add(blob, t_ms, db, from_peer=None, to_peer=None):
    # NAT check only happens if both peers specified
    if from_peer and to_peer:
        if not can_deliver(from_peer, to_peer, t_ms):
            return None  # NAT blocked
```

**Result:** All sync traffic bypasses NAT - peers behind NAT receive packets freely.

---

## Architecture Analysis

### Where peer identity IS known:

| Location | Has `from_peer_id` | Has `to_peer_shared_id` |
|----------|-------------------|------------------------|
| `sync.send_request()` | Yes (parameter) | Yes (parameter) |
| `sync.send_request_to_connection()` | Yes | **No** (only has transit_key) |
| `sync._project_sync_event()` | Yes (recorded_by) | Yes (from request's signed_by) |
| `sync_connect.send()` | Yes (parameter) | Yes (parameter) |
| `sync_connect.project()` | Yes (recorded_by) | Yes (from event's signed_by) |

### The Gap:

`sync_connections` table currently stores:
- `our_transit_key_id` - the key we gave them
- `our_peer_id` - which local peer owns this connection
- `their_transit_key` / `their_transit_key_id` - key they gave us

**Missing:** `their_peer_shared_id` - who we're talking to

This is intentional for privacy, but we need it for NAT simulation.

---

## Proposed Solution

### 1. Schema Change

Add `their_peer_shared_id` to `sync_connections`:

```sql
-- In events/network/sync_connect.sql
ALTER TABLE sync_connections ADD COLUMN their_peer_shared_id TEXT;
```

This column is:
- **Nullable** for backwards compatibility
- **Simulation/debugging only** - not used for crypto or routing
- **Populated when connection is established** (not required for operation)

### 2. sync_connect.py Changes

**In `send()`:** Store peer identity when creating connection

```python
# After line 336 (INSERT INTO sync_connections):
unsafedb.execute("""
    INSERT OR REPLACE INTO sync_connections
    (our_transit_key_id, our_peer_id, their_peer_shared_id, last_seen_ms, ttl_ms)
    VALUES (?, ?, ?, ?, ?)
""", (transit_key_id, from_peer_id, to_peer_shared_id, t_ms, 300000))
```

**In `project()`:** Store peer identity from incoming event

```python
# After line 455 (INSERT INTO sync_connections):
their_peer_shared_id = event_data.get('signed_by')  # Already extracted
# Include in INSERT
```

### 3. sync.py Changes

**In `send_request()`:** Pass peer IDs to queue (lines 954-957)

```python
# Current:
request_blob = crypto.wrap(canonical, to_key, db)
queues.incoming.add(request_blob, t_ms, db)

# Changed:
request_blob = crypto.wrap(canonical, to_key, db)
queues.incoming.add(request_blob, t_ms, db, from_peer=from_peer_id, to_peer=to_peer_shared_id)
```

**In `send_request_to_connection()`:** Look up peer identity (around line 831)

```python
# Look up their_peer_shared_id from connection
conn_info = unsafedb.query_one("""
    SELECT their_peer_shared_id FROM sync_connections
    WHERE our_transit_key_id = ?
""", (our_transit_key_id,))
their_peer_shared_id = conn_info['their_peer_shared_id'] if conn_info else None

# Pass to queue
queues.incoming.add(request_blob, t_ms, db,
                   from_peer=from_peer_id,
                   to_peer=their_peer_shared_id)
```

**In `_project_sync_event()`:** Extract sender and pass to response (around line 1218)

```python
# The sync request contains signed_by - the requester's peer_shared_id
requester_peer_shared_id = sync_data.get('signed_by')

# When sending response:
queues.incoming.add(wrapped_blob, t_ms, db,
                   from_peer=recorded_by,
                   to_peer=requester_peer_shared_id)
```

### 4. sync_connect.py Response Sending

**In `send_connect_ack()`:** (line 526)

```python
# Current:
queues.incoming.add(wrapped, t_ms, db)

# Changed - but we don't know their_peer_shared_id here
# The ack is encrypted to their_transit_key, so it's already authenticated
# NAT mapping should already exist because they sent us the sync_connect first
# For simulation, we can look it up or skip NAT check for acks
```

---

## What NOT to Do

1. **Don't require network.py for all sends**
   - That would be a massive refactor
   - The queue abstraction works, we just need to pass peer IDs

2. **Don't change queue interface fundamentally**
   - The `from_peer`/`to_peer` optional params already exist
   - Just need to use them

3. **Don't track peer identity everywhere**
   - Only in `sync_connections` where it's needed for connection-based sends
   - Direct sends already have peer_id params

4. **Don't break privacy model**
   - `peer_shared_id` is already public (it's in events)
   - Storing it locally doesn't expose anything new

5. **Don't make NAT simulation mandatory**
   - If `from_peer`/`to_peer` not provided, skip NAT check (current behavior)
   - Allows incremental adoption

---

## Implementation Order

1. **Schema migration** - Add `their_peer_shared_id` column
2. **sync_connect.py** - Store peer identity in both send() and project()
3. **sync.py send_request()** - Pass peer IDs (already known)
4. **sync.py send_request_to_connection()** - Look up peer ID, pass to queue
5. **sync.py _project_sync_event()** - Pass peer IDs for responses
6. **Tests** - Update integration test to verify NAT blocking works

---

## Testing Strategy

### Test 1: Verify NAT blocks unsolicited sync

```python
def test_nat_blocks_unsolicited_sync():
    # Alice is public, Bob is behind NAT
    # Alice tries to send sync request to Bob (no prior contact)
    # Request should be dropped by NAT
```

### Test 2: Verify NAT allows response after request

```python
def test_nat_allows_sync_response():
    # Bob (behind NAT) sends sync request to Alice
    # This creates NAT mapping
    # Alice's response should get through
```

### Test 3: Full hole punch flow

```python
def test_nat_hole_punch_enables_sync():
    # Bob and Charlie both behind NAT
    # Alice introduces them (intro event)
    # IntroProcessJob triggers sync_connect from both
    # Now they can sync with each other
```

---

## Alternative Considered: Connection-Level NAT

Instead of tracking `their_peer_shared_id`, we could simulate NAT at the connection level:

- A connection's `(our_transit_key_id, their_transit_key_id)` IS the NAT mapping
- If connection exists and is active, packets flow
- No peer identity needed

**Why rejected:**
- Packets arrive as blobs - we don't know which connection until after unwrap
- NAT check happens BEFORE unwrap (in queue.add)
- Would require restructuring the receive pipeline
- Peer-level NAT matches reality better (real NAT is IP-based)

---

## Notes

- `peer_shared_id` is used for NAT instead of `peer_id` because:
  - It's the public identity (what peers use to address each other)
  - `peer_id` is local/private
  - NAT simulation is about network reachability, which uses public addresses

- The sync_connections table already has `our_peer_id` for local routing
- Adding `their_peer_shared_id` completes the connection's endpoint info
