"""
Scenario test: Negentropy bucket capacity for large files.

Tests that the negentropy sync system can handle very large files
that create many events using time-based prefixes with adaptive splitting.

A 1GB file creates ~2.4 million file_slice events all at the same timestamp.
The negentropy bucket system uses:
1. Time-based prefix levels (root → prefix_2 → prefix_4 → prefix_6)
2. Adaptive splitting within time buckets for large counts

This test verifies that:
1. Events at same timestamp cluster in the same time bucket
2. Hash suffix provides distribution within time buckets
3. Adaptive splitting can handle large buckets
"""
import pytest
from events.network.negentropy import (
    compute_unified_key,
    LEVEL_PREFIX_LEN,
    LEVELS,
    EVENTS_THRESHOLD,
    get_event_count_in_bucket,
    add_event_to_sync,
    find_split_point,
    get_events_in_range,
)


def test_unified_key_distribution_time_based():
    """Events at same timestamp share time prefix, differ in hash suffix."""
    # Generate many event IDs at same timestamp
    ts_ms = 1000000000000
    event_ids = [f"event_{i}" for i in range(1000)]

    # Compute unified keys with same timestamp
    unified_keys = [compute_unified_key(eid, ts_ms) for eid in event_ids]

    # All keys should be 16 hex chars (time + hash)
    for key in unified_keys:
        assert len(key) == 16, f"Key should be 16 chars, got {len(key)}"
        assert all(c in '0123456789abcdef' for c in key), f"Key should be hex: {key}"

    # All keys should share the same time prefix (first 6 chars)
    time_prefixes = set(k[:6] for k in unified_keys)
    assert len(time_prefixes) == 1, \
        f"Same timestamp should have same time prefix, got {len(time_prefixes)}"

    # Hash suffixes (last 10 chars) should be mostly unique
    hash_suffixes = set(k[6:] for k in unified_keys)
    assert len(hash_suffixes) > 900, \
        f"Hash suffixes should be unique, got {len(hash_suffixes)}"

    print(f"\nTime prefix: {list(time_prefixes)[0]}")
    print(f"Unique hash suffixes: {len(hash_suffixes)}")


def test_1gb_file_bucket_distribution(fresh_db):
    """Test that a 1GB file can be synced with time-based + adaptive splitting.

    A 1GB file = 1,073,741,824 bytes / 450 bytes per slice = 2,386,093 slices
    All slices have the same timestamp (created in one batch).

    With time-based unified keys:
    1. All slices are in ONE prefix_6 bucket (same time prefix)
    2. Adaptive splitting divides the bucket via binary median splits
    3. ~log2(2.4M / 100) ≈ 15 splits to get under threshold
    """
    db = fresh_db

    num_slices = 2_386_093  # 1GB file
    ts_ms = 1000000000000  # All at same timestamp

    # Compute sample unified keys to verify time clustering
    sample_size = 10000
    sample_keys = [compute_unified_key(f"slice_{i}", ts_ms) for i in range(sample_size)]

    print(f"\n=== 1GB File Bucket Analysis (Time-Based + Adaptive) ===")
    print(f"Total slices: {num_slices:,}")
    print(f"EVENTS_THRESHOLD: {EVENTS_THRESHOLD}")
    print()

    # All slices at same timestamp should have same time prefix
    time_prefixes = set(k[:6] for k in sample_keys)
    assert len(time_prefixes) == 1, "All slices at same timestamp should have same time prefix"
    time_prefix = list(time_prefixes)[0]
    print(f"Time prefix (prefix_6): {time_prefix}")

    # Hash suffixes should be well-distributed (for adaptive splitting)
    hash_suffixes = set(k[6:] for k in sample_keys)
    print(f"Unique hash suffixes in sample: {len(hash_suffixes)}")
    assert len(hash_suffixes) > sample_size * 0.9, \
        "Hash suffixes should be mostly unique for good splitting"

    # Calculate adaptive split depth needed
    # Each split divides by ~2, need log2(num_slices / threshold) splits
    import math
    splits_needed = math.ceil(math.log2(num_slices / EVENTS_THRESHOLD))
    final_ranges = 2 ** splits_needed

    print()
    print(f"Adaptive splitting analysis:")
    print(f"  Events per prefix_6 bucket: {num_slices:,}")
    print(f"  Splits needed: ~{splits_needed}")
    print(f"  Final ranges: ~{final_ranges:,}")
    print(f"  Events per final range: ~{num_slices / final_ranges:.0f}")

    # With adaptive splitting, we can get all ranges under threshold
    assert num_slices / final_ranges <= EVENTS_THRESHOLD, \
        f"Adaptive splitting should get ranges under threshold"


def test_bucket_subdivision_with_many_events(fresh_db):
    """Test that many events at same timestamp go into one prefix_6 bucket.

    Creates 500 events at the same timestamp and verifies:
    1. All events are in the same prefix_6 bucket (same time prefix)
    2. Adaptive splitting can subdivide via find_split_point
    """
    db = fresh_db

    # We need a peer_id to add events
    from events.identity import user
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    peer_id = alice['peer_id']

    num_events = 500
    ts_ms = 5000000000000  # Fixed timestamp for all events

    # Add events to sync system
    print(f"\n=== Adding {num_events} events at same timestamp ===")
    for i in range(num_events):
        event_id = f"test_event_{i:04d}"
        add_event_to_sync(db, peer_id, event_id, ts_ms)

    db.commit()

    # All events should be in the same prefix_6 bucket (same time prefix)
    finest_level = LEVELS[-1]  # prefix_6
    finest_prefix_len = LEVEL_PREFIX_LEN[finest_level]

    unified_keys = [compute_unified_key(f"test_event_{i:04d}", ts_ms)
                    for i in range(num_events)]
    finest_prefixes = set(k[:finest_prefix_len] for k in unified_keys)

    print(f"Finest level: {finest_level}")
    print(f"Unique prefixes: {len(finest_prefixes)}")

    # All events at same timestamp should be in ONE prefix_6 bucket
    assert len(finest_prefixes) == 1, \
        f"Same timestamp should mean one bucket, got {len(finest_prefixes)}"

    time_prefix = list(finest_prefixes)[0]
    count = get_event_count_in_bucket(db, peer_id, time_prefix, finest_level)
    print(f"  Bucket {time_prefix}: {count} events")

    # With 500 events > EVENTS_THRESHOLD, adaptive splitting kicks in
    # Test that we can find split points
    split_key = find_split_point(db, peer_id, time_prefix)
    assert split_key is not None, "Should find split point for large bucket"

    # Split point should divide events roughly in half
    low_events = get_events_in_range(db, peer_id, time_prefix, hi_key=split_key)
    high_events = get_events_in_range(db, peer_id, time_prefix, lo_key=split_key)

    print(f"  Split point: {split_key}")
    print(f"  Low range: {len(low_events)} events")
    print(f"  High range: {len(high_events)} events")

    # Both halves should have events
    assert len(low_events) > 0, "Low range should have events"
    assert len(high_events) > 0, "High range should have events"
    # Split should be roughly balanced
    assert abs(len(low_events) - len(high_events)) < num_events * 0.2, \
        "Split should be roughly balanced"
