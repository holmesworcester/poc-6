"""
Scenario test: Negentropy bucket capacity for large files.

Tests that the negentropy sync system can handle very large files
that create many events.

A 1GB file creates ~2.4 million file_slice events.
The negentropy bucket system uses time+hash bucketing:
- Time prefix (6 hex chars): clusters events by creation time
- Hash suffix (10 hex chars): provides uniqueness within time buckets

For file slices (same created_at), they cluster in the same time bucket.
This is intentional - sync transfers them together atomically.

This test verifies that:
1. Events with same timestamp cluster in the same time bucket (prefix_6)
2. Events with different timestamps spread across time buckets
3. Hash suffix provides uniqueness for events with same timestamp
"""
import pytest
from events.network.negentropy import (
    compute_unified_key,
    LEVEL_PREFIX_LEN,
    LEVELS,
    EVENTS_THRESHOLD,
    get_event_count_in_bucket,
    add_event_to_sync,
)


def test_unified_key_time_clustering():
    """Events with same timestamp cluster in same time bucket."""
    # Generate many event IDs with same timestamp (like file slices)
    timestamp = 1718451045000  # June 2024
    event_ids = [f"event_{i}" for i in range(1000)]

    # Compute unified keys with same timestamp
    unified_keys = [compute_unified_key(eid, timestamp) for eid in event_ids]

    # All keys should be 16 hex chars
    for key in unified_keys:
        assert len(key) == 16, f"Key should be 16 chars, got {len(key)}"
        assert all(c in '0123456789abcdef' for c in key), f"Key should be hex: {key}"

    # All keys should have the SAME time prefix (first 6 chars)
    time_prefixes = set(k[:6] for k in unified_keys)
    assert len(time_prefixes) == 1, \
        f"Events with same timestamp should have same time prefix, got {len(time_prefixes)}"

    # But all keys should be unique (different event_ids give different hash suffix)
    unique_keys = set(unified_keys)
    assert len(unique_keys) == 1000, f"All keys should be unique, got {len(unique_keys)}"

    # Hash suffixes (last 10 chars) should all be unique
    hash_suffixes = set(k[6:] for k in unified_keys)
    assert len(hash_suffixes) == 1000, f"Hash suffixes should be unique, got {len(hash_suffixes)}"

    print(f"\nTime prefix (shared by all): {list(time_prefixes)[0]}")
    print(f"Sample hash suffixes: {list(hash_suffixes)[:5]}")


def test_unified_key_time_distribution():
    """Events with different timestamps spread across time buckets."""
    # Generate events with different timestamps (1 hour apart)
    base_ts = 1718451045000  # June 2024
    hour_ms = 60 * 60 * 1000
    events = [(f"event_{i}", base_ts + i * hour_ms) for i in range(100)]

    # Compute unified keys
    unified_keys = [compute_unified_key(eid, ts) for eid, ts in events]

    # Time prefixes should be different for events hours apart
    time_prefixes = set(k[:6] for k in unified_keys)
    print(f"\nUnique time prefixes for 100 events (1hr apart): {len(time_prefixes)}")
    print(f"Sample prefixes: {list(time_prefixes)[:5]}")

    # With ~4.4 min granularity, 1 hour apart should give different prefixes
    # 100 events 1hr apart = 100 different time prefixes
    assert len(time_prefixes) == 100, \
        f"Events 1hr apart should have different time prefixes, got {len(time_prefixes)}"


def test_1gb_file_bucket_distribution(fresh_db):
    """Test that a 1GB file's slices cluster in same time bucket.

    A 1GB file = 1,073,741,824 bytes / 450 bytes per slice = 2,386,093 slices

    With time+hash unified keys, all slices (same created_at) cluster in
    the same time bucket. This is by design - they sync together atomically.
    """
    db = fresh_db

    num_slices = 2_386_093  # 1GB file
    file_created_at = 1718451045000  # All slices have same timestamp

    # Calculate distribution with time+hash keys
    sample_size = 10000
    sample_keys = [compute_unified_key(f"slice_{i}", file_created_at)
                   for i in range(sample_size)]

    print(f"\n=== 1GB File Bucket Analysis (Time+Hash) ===")
    print(f"Total slices: {num_slices:,}")
    print(f"Sample size: {sample_size:,}")
    print(f"File created_at: {file_created_at}")
    print(f"EVENTS_THRESHOLD: {EVENTS_THRESHOLD}")
    print()

    # All samples should share the same time prefix
    time_prefix = sample_keys[0][:6]
    assert all(k[:6] == time_prefix for k in sample_keys), \
        "All file slices should have same time prefix"

    print(f"Time prefix (shared by all slices): {time_prefix}")

    for level in LEVELS:
        prefix_len = LEVEL_PREFIX_LEN[level]
        if prefix_len == 0:
            unique_prefixes = 1  # root
        else:
            prefixes = set(k[:prefix_len] for k in sample_keys)
            unique_prefixes = len(prefixes)

        print(f"{level:12s} (prefix_len={prefix_len:2d}): {unique_prefixes:>6,} unique prefixes")

    # At prefix_6 (24 bits = 6 hex chars), all slices are in ONE bucket
    # because they share the same time prefix
    finest_level = LEVELS[-1]  # prefix_6
    finest_prefixes = set(k[:6] for k in sample_keys)
    assert len(finest_prefixes) == 1, \
        f"All file slices should be in same prefix_6 bucket, got {len(finest_prefixes)}"

    print(f"\nAll {num_slices:,} slices cluster in time bucket: {time_prefix}")
    print("This is intentional - file slices sync atomically together")


def test_bucket_subdivision_across_time(fresh_db):
    """Test that events across different times spread across buckets.

    Creates 500 events with different timestamps and verifies they
    spread across multiple time buckets.
    """
    db = fresh_db

    # We need a peer_id to add events
    from events.identity import user
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    peer_id = alice['peer_id']

    num_events = 500
    base_ts = 1718451045000
    hour_ms = 60 * 60 * 1000

    # Add events with different timestamps (1 hour apart)
    print(f"\n=== Adding {num_events} events (1hr apart) ===")
    for i in range(num_events):
        event_id = f"test_event_{i:04d}"
        created_at = base_ts + i * hour_ms
        add_event_to_sync(db, peer_id, event_id, created_at)

    db.commit()

    # Check event distribution at finest level
    finest_level = LEVELS[-1]
    finest_prefix_len = LEVEL_PREFIX_LEN[finest_level]

    # Get all unique prefixes at finest level
    unified_keys = [compute_unified_key(f"test_event_{i:04d}", base_ts + i * hour_ms)
                    for i in range(num_events)]
    finest_prefixes = set(k[:finest_prefix_len] for k in unified_keys)

    print(f"Finest level: {finest_level}")
    print(f"Unique time buckets: {len(finest_prefixes)}")

    # Check actual bucket counts from database
    for prefix in list(finest_prefixes)[:5]:  # Sample first 5
        count = get_event_count_in_bucket(db, peer_id, prefix, finest_level)
        print(f"  Bucket {prefix}: {count} events")

    # With events 1hr apart and ~4.4min granularity, each event gets its own bucket
    assert len(finest_prefixes) == num_events, \
        f"Events 1hr apart should each have own time bucket, got {len(finest_prefixes)}"
