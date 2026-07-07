# Adaptive Splitting Status

## Overview

This branch implements adaptive binary splitting for the negentropy sync protocol to handle large files efficiently. The goal is to avoid "range explosion" when syncing files with many events at the same timestamp.

## The Problem

A 10MB file creates ~23,000 `file_slice` events, all at the same timestamp. With the original prefix-based drilling approach:
- Events would be drilled down through prefix levels (prefix_2 → prefix_4 → ... → prefix_10)
- At prefix_10, we'd have ~23,000 individual ranges to reconcile
- This causes exponential message overhead

## The Solution: Adaptive Splitting

Instead of drilling to fine prefix levels, we:
1. Use time-based prefix tree up to `prefix_6` (6 hex chars = ~4.4 minute buckets)
2. At `prefix_6`, if a bucket has > 100 events (EVENTS_THRESHOLD), switch to adaptive binary splitting
3. Find the median unified_key and split into two child ranges: `[lo, median)` and `[median, hi)`
4. Continue splitting until each range has ≤ 100 events
5. Then send blobs directly

**Expected efficiency:**
- 23,000 events / 100 per range = 233 final ranges
- log₂(233) ≈ 8 binary splits to discover all ranges
- Much better than 23,000 prefix-based ranges

## Current Implementation Status

### What's Working
- Unified key format: `time_hex (6 chars) + hash (10 chars)`
- XOR fingerprinting for O(1) bucket hash updates
- Adaptive split detection and median finding
- Wire format with `lo_key/hi_key` bounds
- Small file sync (1KB test passes)

### What's Broken: Large File Sync

The 10MB sync test shows:
```
Round  90: slices=0/23,302 (0.0%) neg_events=22 incoming=11182(neg=7920,other=3262) neg_ranges=4100(sent=146)
```

**Diagnosis:**
- `neg_ranges=4100` with `sent=146` means ranges are being created and some marked as "events sent"
- `incoming=11182` with `other=3262` means ~3,262 non-negentropy events are in the incoming queue
- But `neg_events=22` (Bob's negentropy_events table) isn't growing
- And `slices=0` means no file slices are reaching Bob's projection tables

**The bottleneck:** Events are in `incoming_event_log` but not being materialized to Bob's tables.

### Root Cause Analysis

1. **Protocol flow issue discovered:** The original protocol assumed "requester controls subdivision" - but when the requester (Bob) has no data, he can't compute split points.

2. **Fix attempted:** Modified `handle_range_request` to include `split_key` and `child_hashes` in `range_mismatched` response, so the requester can subdivide using the responder's split points.

3. **Remaining issue:** The blob events sent by Alice aren't being processed by Bob's projection pipeline. They're stuck in `incoming_event_log`.

## Key Code Changes

### New Message Types
```python
MSG_RANGE_BLOBS_SENT = 4   # "I sent you blobs for this range"
MSG_RANGE_MISMATCHED = 5   # "Hashes differ, here's how to split"
```

### Modified Functions
- `handle_range_request`: Now includes `split_key` in mismatched response
- `handle_range_mismatched`: Uses responder's `split_key` when requester has no data
- `handle_range_blobs_sent`: New handler for blob notification
- `encode_wire_event` / `decode_wire_event`: Support new message types

### Wire Format Changes
- Added `bounds_flags`, `lo_key`, `hi_key` fields for adaptive ranges
- Added `split_key` and `child_hashes` to mismatched response

## Next Steps to Debug

1. **Check why blobs aren't materializing:**
   - Are the blob events valid? (correct format, signatures, etc.)
   - Is `SyncUpdateJob` processing them?
   - Are there projection errors?

2. **Add logging to:**
   - `_send_event_blobs` to confirm blobs are being sent
   - `SyncUpdateJob.run` to see what's being processed
   - Projection pipeline to catch errors

3. **Verify blob format:**
   - Are file_slice events being wrapped correctly?
   - Is the transit encryption working for Bob to decrypt?

## Test Files

- `tests/scenario_tests/test_10mb_file_sync.py` - End-to-end 10MB sync test (currently failing)
- `tests/scenario_tests/test_large_file_negentropy_buckets.py` - Unit tests for bucket math (passing)
- `tests/scenario_tests/test_file_attachment_sync_only.py` - 1KB file sync (passing)

## Performance Expectations (Once Fixed)

For a 10MB file (23,302 slices):
- ~8 rounds to discover the split structure
- ~233 final ranges, each with ~100 events
- Blobs sent in batches per range
- Total sync: ~20-30 rounds (vs current 100+ with no progress)
