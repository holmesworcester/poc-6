# 6X Sync Throughput Improvement Plan

## Current State (50ms RTT)

| Test | Rounds | Sim Time | Throughput |
|------|--------|----------|------------|
| 1k Messages | 27 | 2.7s | 370 msg/s |
| 1MB File | 30 | 3.0s | 0.33 MB/s |
| 10MB File | 241 | 12.1s | 0.83 MB/s |

## Target (6x improvement)

| Test | Rounds | Sim Time | Throughput |
|------|--------|----------|------------|
| 1k Messages | 5 | 0.45s | 2,200 msg/s |
| 1MB File | 5 | 0.5s | 2 MB/s |
| 10MB File | 40 | 2.0s | 5 MB/s |

## Bottleneck Analysis

```
23,302 slices / 50 events per leaf = 466 theoretical leaf rounds
Current: 241 rounds → CC window provides ~2x parallelism
Target: 40 rounds → need 583 events/round
```

---

## Phase 1: Easy Wins (Target: 2-3x)

### 1.1 Increase EVENTS_THRESHOLD
```python
# Current
EVENTS_THRESHOLD = 50

# Change to
EVENTS_THRESHOLD = 200
```

**Impact:** 23,302 / 200 = 117 leaf rounds (2x improvement)
**Risk:** Larger messages, more retransmit cost on packet loss
**Difficulty:** TRIVIAL

### 1.2 Tune Congestion Control
```python
# Current
CC_MIN_WINDOW = 1
CC_MAX_WINDOW = 32

# Change to
CC_INITIAL_WINDOW = 4
CC_MAX_WINDOW = 64
```

**Impact:** 4-8 parallel ranges × 200 events = 800-1600 events/round
**Risk:** Congestion on lossy networks
**Difficulty:** EASY

---

## Phase 2: Protocol Optimizations (Target: 4-5x)

### 2.1 Skip-to-Leaf on Bisect

When bisecting in Case 4 and one half has events:
1. Send ZERO for empty half (peer will send us events)
2. For non-empty half: skip directly to first leaf, send events

```python
# In handle_v2_range Case 4 bisect:
if left_hash == ZERO_HASH:
    # Empty - send ZERO
    responses.append({'type': 'v2_range', 'start': start, 'end': mid, 'hash': ZERO})
else:
    # Skip to first leaf
    leaf_start, leaf_end = first_leaf_in_range(db, recorded_by, start, mid)
    events = get_events_in_range(db, recorded_by, leaf_start, leaf_end)
    send_event_blobs(events)
    responses.append({'type': 'v2_range', ...})
```

**Impact:** ~5 fewer bisection rounds for clustered data
**Difficulty:** MEDIUM

### 2.2 Parallel Blob Fetching

Current: Blobs sent sequentially after negentropy identifies them
Change: Pipeline blob requests, send multiple per tick

**Impact:** Overlaps blob transfer with range discovery
**Difficulty:** MEDIUM

---

## Phase 3: Major Changes (Target: 6x)

### 3.1 Multi-Range Messages

Batch multiple ranges into single message:
```python
# Instead of 1 range per message:
{'type': 'v2_range', 'start': X, 'end': Y, 'hash': H}

# Batch 10 ranges:
{'type': 'v2_ranges', 'ranges': [
    {'start': X1, 'end': Y1, 'hash': H1},
    {'start': X2, 'end': Y2, 'hash': H2},
    ...
]}
```

**Impact:** 10x more data per round-trip
**Risk:** Wire format change, message size limits
**Difficulty:** HARD

### 3.2 Aggressive Pipelining

Don't wait for response before sending next request.
Send all pending ranges immediately up to bandwidth limit.

**Difficulty:** HARD

---

## Projection Limit Verification

At 6x throughput:
- 23,302 events in 2 seconds = 11,651 events/sec
- Current wall time: 4s for 12s sim = ~6,000 events/sec processing
- May need projection optimizations if CPU-bound

---

## Implementation Order

1. [ ] **Phase 1.1:** EVENTS_THRESHOLD 50 → 200
2. [ ] **Phase 1.2:** CC_INITIAL_WINDOW = 4, CC_MAX_WINDOW = 64
3. [ ] Benchmark Phase 1
4. [ ] **Phase 2.1:** Skip-to-leaf optimization
5. [ ] **Phase 2.2:** Parallel blob fetching
6. [ ] Benchmark Phase 2
7. [ ] **Phase 3:** If needed for 6x target
