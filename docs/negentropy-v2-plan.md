# Negentropy V2: Binary Bisection Protocol

## Problem Statement

Current negentropy implementation has critical inefficiencies:

1. **256-way fan-out**: Drilling from prefix_8 to prefix_10 sends up to 65,536 range requests for a 1GB file
2. **No real protocol**: Cold start just blasts all events, hopes they arrive
3. **Restarts every second**: No session continuity, progress lost
4. **No packet loss recovery**: Lost packets discovered only on next full sync cycle

## Proposed Solution: Stateless Binary Bisection

### Core Principles

1. **Ranges are self-describing**: Each message contains (start, end, hash)
2. **Binary bisection**: Split ranges in half, not 256-way
3. **Stateless ping-pong**: No session state needed, range IS the state
4. **Explicit responses**: Every message gets a response (no silence-as-ACK ambiguity)

### Message Format

```python
@dataclass
class RangeMessage:
    start: str      # 12 hex chars (48-bit unified key)
    end: str        # 12 hex chars (exclusive)
    hash: bytes     # 16-byte XOR fingerprint, or ZERO (b'\x00'*16)
    events: list[bytes] = None  # Optional: event blobs for this range
```

### Protocol Rules

```
RECEIVE (start, end, their_hash, their_events):

  # Store any events they sent
  IF their_events:
    store(their_events)

  our_hash = compute_xor_hash(start, end)

  CASE: their_hash == our_hash
    # Match - explicit ACK
    SEND (start, end, MATCH)

  CASE: their_hash == ZERO and our_hash == ZERO
    # Both empty - explicit ACK
    SEND (start, end, MATCH)

  CASE: their_hash == ZERO and our_hash != ZERO
    # They have nothing, we have events - send from first leaf
    leaf_start, leaf_end = first_leaf(start, end)
    next_start = leaf_end

    SEND (leaf_start, leaf_end, our_leaf_hash, events_in_leaf)
    SEND (next_start, end, our_rest_hash)

  CASE: our_hash == ZERO and their_hash != ZERO
    # We have nothing, they have events - request
    SEND (start, end, ZERO)

  CASE: our_hash != their_hash (both non-zero)
    # Mismatch - bisect or send events
    count = count_events(start, end)

    IF count <= THRESHOLD:
      SEND (start, end, our_hash, our_events)
    ELSE:
      mid = midpoint(start, end)
      SEND (start, mid, left_hash)
      SEND (mid, end, right_hash)
```

### Key Design Decisions

#### 1. Binary vs N-way Bisection

| Approach | Messages per level | Rounds for 64K buckets |
|----------|-------------------|------------------------|
| 256-way (current) | 256 | 1 |
| Binary | 2 | 16 |

With RTT-limited transport (CC window=1), binary is faster:
- 256-way: 1 round × 256 messages = 256 messages processed serially
- Binary: 16 rounds × 2 messages = 32 messages total

#### 2. Explicit MATCH vs Silence

Explicit MATCH chosen because:
- Unambiguous: silence could mean match, packet loss, or peer crash
- Enables timeout-based retry without false positives
- Simplifies initiator logic

#### 3. Leaf Selection for ZERO Response

When they have nothing, we pick the "first leaf" in the range:
- Leaf = smallest range where count <= THRESHOLD
- Deterministic: both sides agree on what "first" means
- Enables pipelining: send leaf events + next range in one response

#### 4. Range Representation

Using unified key hex strings (12 chars = 48 bits):
- `start` inclusive, `end` exclusive: [start, end)
- Root range: ["000000000000", "1000000000000") - note: 13 chars for max+1
- Midpoint: numeric average, formatted back to hex

### Wire Format Changes

Current negentropy wire format (344 bytes) needs modification:

```
New layout:
- connection_id (16 bytes)
- reply_connection_id (16 bytes)
- msg_type (1 byte): RANGE_REQUEST=1, RANGE_MATCH=2
- range_start (6 bytes) - 48 bits
- range_end (6 bytes) - 48 bits
- hash (16 bytes) - XOR fingerprint or ZERO
- event_count (2 bytes) - number of events included
- events (variable) - event blobs

Total fixed: 63 bytes + variable events
```

For large event transfers, use separate blob messages (existing mechanism).

### State Machine

Initiator tracks "open ranges" locally:

```python
open_ranges: set[tuple[str, str]] = set()

def initiate_sync():
    send(ROOT_START, ROOT_END, our_root_hash)
    open_ranges.add((ROOT_START, ROOT_END))

def on_receive(start, end, their_hash, their_events):
    open_ranges.discard((start, end))
    # Process response per protocol rules
    # Add new ranges to open_ranges as needed

def on_timeout():
    for (start, end) in open_ranges:
        resend(start, end, our_hash)

def is_sync_complete():
    return len(open_ranges) == 0
```

### Example Flows

#### Cold Start (A has 1000 events, B has 0)

```
B→A: (0, MAX, ZERO)
A→B: events[0:50] + (0, L1, hash0) + (L1, MAX, hash_rest)
B→A: (0, L1, MATCH) + (L1, MAX, ZERO)
A→B: events[50:100] + (L1, L2, hash1) + (L2, MAX, hash_rest)
...
Final: B→A: (Ln, MAX, MATCH)
```

Rounds: ~20 for 1000 events (1000/50 = 20 leaves)

#### Partial Sync (A has 1000, B has 999, missing event J)

```
A→B: (0, MAX, hash_A)
B→A: (0, MAX, hash_B)  # mismatch
A→B: (0, MID, left) + (MID, MAX, right)
B→A: (0, MID, MATCH) + (MID, MAX, hash_B_right)  # mismatch in right half
...bisect until leaf containing J...
A→B: (J_start, J_end, hash, [J])
B→A: (J_start, J_end, MATCH)
```

Rounds: ~16 (log2 of 64K buckets)

#### Packet Loss Recovery

```
A→B: events + (L1, L2, hash1)  # events arrive, hash message dropped
B stores events, waits...
A times out, resends: (L1, L2, hash1)
B computes hash, matches!
B→A: (L1, L2, MATCH)
```

#### Bidirectional Sync

```
A has {1,2,3,4,5}, B has {1,2,3,4,6}  # A missing 6, B missing 5

A→B: (0, MAX, hash_A)
B→A: (0, MAX, hash_B)  # mismatch
...bisect...
# Find range R5 containing event 5:
A→B: (R5_start, R5_end, hash_A_R5, [event_5])
B→A: (R5_start, R5_end, MATCH)

# Find range R6 containing event 6:
A→B: (R6_start, R6_end, hash_A_R6)  # A has ZERO or different
B→A: (R6_start, R6_end, hash_B_R6, [event_6])
A→B: (R6_start, R6_end, MATCH)
```

### Implementation Plan

#### Phase 1: Core Protocol (this PR)

1. Add `compute_range_hash(start, end)` function
2. Add `midpoint(start, end)` function
3. Add `first_leaf(start, end)` function
4. New message types: RANGE_REQUEST, RANGE_MATCH
5. New handler: `handle_range_message()`
6. Update `sync_connection()` to use binary bisection
7. Add timeout/retry logic with open_ranges tracking

#### Phase 2: Wire Format

1. Define new wire format for range messages
2. Backward compatibility: detect old vs new format
3. Migrate existing connections

#### Phase 3: Testing

1. Unit tests for bisection logic
2. Integration tests for cold start, partial sync, packet loss
3. Performance benchmarks: compare with current implementation

### Performance Comparison

| Scenario | Current (256-way) | New (binary) |
|----------|------------------|--------------|
| 10MB cold start | ~19,400 range msgs | ~460 range msgs |
| 10MB partial (1 missing) | ~19,400 range msgs | ~32 range msgs |
| 1GB cold start | ~65,536 range msgs | ~48,000 range msgs |
| 1GB partial (1 missing) | ~65,536 range msgs | ~32 range msgs |

Note: Cold start with binary is still O(n/threshold) for the leaf traversal, but partial sync is O(log n).

### Open Questions

1. **Threshold value**: Current is 50. Optimal for binary bisection?
2. **Pipelining**: Can we send multiple ranges without waiting for response?
3. **Congestion control**: How does CC interact with binary bisection?
4. **Event batching**: Max events per message? Separate blob channel?

### Risks

1. **Wire format change**: Backward compatibility concerns
2. **Complexity**: More protocol states to handle
3. **Testing**: Need comprehensive edge case coverage

### Success Metrics

1. 10MB file sync completes in <100 range messages (vs current ~19,400)
2. Partial sync of 1 missing event in <50 messages
3. Packet loss recovery without full resync
