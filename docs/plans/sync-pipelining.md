# Sync Protocol Pipelining

## Problem

The sync protocol is **latency-bound**, not bandwidth-bound. Network simulation showed:
- 10 sync rounds at 500ms latency = 10.1 seconds
- Each round requires full RTT before next request can be sent
- With 100ms latency and 10 windows, sync takes ~4 seconds when it could take ~1.5 seconds

## Current Architecture

```
tick(t_ms) → runs jobs in sequence:
  1. ConnectionSendJob (every 1 second)
  2. SyncReceiveJob (every 100ms)        ← Processes incoming responses
  3. FileSyncJob (every 100ms)
  4. NegentropySyncJob (every 1 second)  ← Sends new sync requests
```

Each sync round:
1. Send bloom request for window N
2. **Wait for network RTT**
3. Receive response with events
4. Process events, project dependencies
5. Send bloom request for window N+1
6. **Repeat**

## Pipelining Opportunities

### Phase 1: Quick Wins (Low Risk, 10-20% improvement)

#### 1.1 Batch Response Wrapping

**Location:** `sync.py:1156-1188`

Current:
```python
for event_id in events_to_send:
    event_blob = safedb.get_shareable_blob(event_id)
    wrapped_blob = crypto.wrap(event_blob, transit_key_dict, db)
    queues.incoming.add(wrapped_blob, t_ms, db)
```

Proposed:
```python
# Batch load
event_blobs = [(eid, safedb.get_shareable_blob(eid)) for eid in events_to_send]
# Batch wrap and send
for event_id, blob in event_blobs:
    wrapped_blob = crypto.wrap(blob, transit_key_dict, db)
    queues.incoming.add(wrapped_blob, t_ms, db)
```

Not much gain here since wrapping is sequential anyway, but sets up for:

#### 1.2 Increase Sync Job Frequency

**Location:** `jobs.py`

Current: `NegentropySyncJob` runs every 1000ms
Proposed: Run every 100-200ms

This allows requests to be sent more frequently, reducing idle wait time.

### Phase 2: In-Flight Window Tracking (Medium Effort, 2-3x improvement)

#### 2.1 Multiple Windows Per Connection

Instead of:
```
Request window 0 → wait RTT → Response → Request window 1 → wait RTT → ...
```

Do:
```
Request window 0 → Request window 1 → Request window 2 →
            ← Response 0 ← Response 1 ← Response 2
```

**Implementation:**

Add `in_flight_windows` tracking to sync state:

```python
# New schema addition
CREATE TABLE sync_window_state_ephemeral (
    from_peer_id TEXT,
    to_peer_id TEXT,
    window_id INTEGER,
    request_sent_at_ms INTEGER,
    response_received INTEGER DEFAULT 0,
    PRIMARY KEY (from_peer_id, to_peer_id, window_id)
)
```

**Changes to `send_requests()`:**
```python
MAX_IN_FLIGHT = 3

def send_requests(...):
    for conn in connections:
        # Get windows already in flight
        in_flight = get_in_flight_windows(from_peer, to_peer, safedb)

        if len(in_flight) >= MAX_IN_FLIGHT:
            continue  # Wait for some responses

        # Send up to MAX_IN_FLIGHT - len(in_flight) new requests
        for _ in range(MAX_IN_FLIGHT - len(in_flight)):
            window_id = get_next_unsent_window(...)
            if window_id is None:
                break
            send_sync_request(conn, window_id, ...)
            mark_window_in_flight(from_peer, to_peer, window_id, t_ms)
```

**Changes to `receive()`:**
- Mark window as received when response arrives
- Allow processing out-of-order responses

### Phase 3: Interleaved Send/Receive (Medium Effort, 10-20% improvement)

#### 3.1 Process Responses While Sending

Current job order processes receives separately from sends.

Proposed: Single `PipelinedSyncJob` that:
1. Drains available responses
2. Processes them (projects events)
3. Sends new requests for newly completed windows
4. Repeat until no more work

```python
class PipelinedSyncJob(Job):
    name = 'pipelined_sync'
    every_ms = 100

    def run(self, t_ms, db):
        # Interleave receive and send
        for _ in range(10):  # Max iterations per tick
            # Receive what's available
            received = sync.receive(batch_size=100, t_ms=t_ms, db=db)
            if not received:
                break

            # Send new requests based on what we received
            sync.send_follow_up_requests(t_ms=t_ms, db=db)
```

### Phase 4: Negentropy Parallel Drilling (Higher Effort, 30-50% for large sets)

For large event sets, negentropy does depth-first drilling:
- Query range hash
- If mismatch, drill into sub-ranges
- Repeat until small enough to enumerate

Pipelining: Send multiple range queries at different granularities simultaneously.

```python
# Send queries for year, month, week levels at once
send_range_query(conn, level='year', range=(2023, 2024))
send_range_query(conn, level='month', range=(2024, 1, 6))
send_range_query(conn, level='week', range=(2024, 5, 15, 22))
```

This requires more complex state tracking but dramatically reduces rounds for large syncs.

## Ordering Constraints

**Must preserve:**
1. Connection established before sync (handshake keys needed)
2. Events in store before projection (dependency order)
3. Bloom salt derivation needs responder's pubkey

**Safe to parallelize:**
- Multiple window requests per connection (with separate state tracking)
- Response processing for different blobs (independent)
- Sync receive while other connections are sending

## Implementation Plan

### Step 1: Add in-flight window tracking
- [ ] Add `sync_window_state_ephemeral` table
- [ ] Track window state: pending, in_flight, received
- [ ] Update `send_requests()` to check in-flight count

### Step 2: Enable multiple in-flight windows
- [ ] Add `MAX_IN_FLIGHT` config (default 3)
- [ ] Send up to MAX_IN_FLIGHT windows per connection
- [ ] Handle out-of-order response processing

### Step 3: Create pipelined sync job
- [ ] Merge SyncReceiveJob and NegentropySyncJob
- [ ] Interleave receive/send in single job
- [ ] Increase job frequency to 100ms

### Step 4: Add tests
- [ ] Test multiple in-flight windows converge correctly
- [ ] Test out-of-order response handling
- [ ] Performance test: measure sync time with varying latency

### Step 5: Benchmark
- [ ] Compare sync times: old vs pipelined
- [ ] Test with network_config presets (lan, broadband, satellite)
- [ ] Verify no regression in correctness

## Expected Results

| Scenario | Current | Pipelined (est.) |
|----------|---------|------------------|
| 10 rounds, 100ms RTT | 2000ms | 600-800ms |
| 10 rounds, 500ms RTT | 10s | 3-4s |
| Large sync (1000 events) | 40s | 8-12s |

## Risks

1. **Complexity** - More state to track, harder to debug
2. **Race conditions** - Out-of-order responses need careful handling
3. **Resource usage** - More in-flight requests = more memory

## Alternatives Considered

1. **Just increase batch sizes** - Doesn't help with RTT-bound syncs
2. **Parallel connections** - Already supported, but each connection is still serial
3. **Push-based sync** - Different protocol, larger change
