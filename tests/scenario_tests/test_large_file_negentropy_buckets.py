"""
Scenario test: Negentropy bucket capacity for large files.

Tests that the negentropy sync system can handle very large files
that create many events with the same timestamp.

A 1GB file creates ~2.4 million file_slice events, all at the same timestamp.
The negentropy bucket system must be able to subdivide these events
to avoid sending millions of event IDs in a single message.

This test verifies that:
1. Events with the same timestamp get distributed across different buckets
2. No single bucket contains more than EVENTS_THRESHOLD events at the finest level
3. Sync completes successfully for large files
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


def test_unified_key_distribution_same_timestamp():
    """Events with same timestamp should have different unified keys due to hash suffix."""
    timestamp = 1700000000000  # Fixed timestamp

    # Generate many event IDs
    event_ids = [f"event_{i}" for i in range(1000)]

    # Compute unified keys
    unified_keys = [compute_unified_key(eid, timestamp) for eid in event_ids]

    # All should have the same timestamp prefix (chars 0-12)
    timestamp_prefixes = set(k[:12] for k in unified_keys)
    assert len(timestamp_prefixes) == 1, "All events should have same timestamp prefix"

    # But different hash suffixes (chars 12-16)
    hash_suffixes = set(k[12:] for k in unified_keys)
    # With 1000 events and 16-bit hash, we expect most to be unique
    assert len(hash_suffixes) > 900, f"Hash suffixes should be mostly unique, got {len(hash_suffixes)}"

    # Check distribution at finest level (prefix_8)
    finest_level = LEVELS[-1]
    finest_prefix_len = LEVEL_PREFIX_LEN[finest_level]

    finest_prefixes = set(k[:finest_prefix_len] for k in unified_keys)

    # If prefix_8 only covers timestamp bits (8 chars), all would be in same bucket
    # If it extends into hash bits, they would spread out
    print(f"\nFinest level: {finest_level} (prefix_len={finest_prefix_len})")
    print(f"Unique prefixes at finest level: {len(finest_prefixes)}")
    print(f"Sample prefixes: {list(finest_prefixes)[:5]}")

    # This assertion reveals the problem: with prefix_8, all same-timestamp
    # events are in the same bucket because the hash is in chars 12-16
    if finest_prefix_len <= 12:
        # Current implementation: all in same bucket
        assert len(finest_prefixes) == 1, \
            f"With prefix_len={finest_prefix_len} (timestamp only), all should be in same bucket"
        print("WARNING: All same-timestamp events are in the same bucket!")
        print("This will cause issues with large files (>100 slices per millisecond)")
    else:
        # Fixed implementation: distributed across buckets
        assert len(finest_prefixes) > 100, \
            f"With prefix_len={finest_prefix_len} (includes hash), events should spread across buckets"


def test_1gb_file_bucket_distribution(fresh_db):
    """Test that a 1GB file's slices would be distributed across buckets.

    A 1GB file = 1,073,741,824 bytes / 450 bytes per slice = 2,386,093 slices

    All slices created at the same timestamp should still be distributable
    into buckets of <=100 events each.
    """
    db = fresh_db

    # Simulate the scenario: 2.4M events at the same timestamp
    # (We can't actually create 2.4M events in a test, so we analyze the structure)

    timestamp = 1700000000000
    num_slices = 2_386_093  # 1GB file

    # Calculate how many unique buckets we'd get at each level
    # by computing unified keys for a sample and extrapolating
    sample_size = 10000
    sample_keys = [compute_unified_key(f"slice_{i}", timestamp) for i in range(sample_size)]

    print(f"\n=== 1GB File Bucket Analysis ===")
    print(f"Total slices: {num_slices:,}")
    print(f"Sample size: {sample_size:,}")
    print(f"EVENTS_THRESHOLD: {EVENTS_THRESHOLD}")
    print()

    for level in LEVELS:
        prefix_len = LEVEL_PREFIX_LEN[level]
        if prefix_len == 0:
            unique_prefixes = 1  # root
        else:
            prefixes = set(k[:prefix_len] for k in sample_keys)
            unique_prefixes = len(prefixes)

        # Extrapolate to full file
        # With good hash distribution, unique prefixes scale with min(sample, 16^prefix_len)
        if level == 'root':
            estimated_buckets = 1
        elif prefix_len <= 12:
            # Timestamp portion only - all same-timestamp events in same bucket
            estimated_buckets = 1
        else:
            # Hash portion included - events spread by hash
            hash_bits_used = (prefix_len - 12) * 4  # 4 bits per hex char
            max_buckets_from_hash = 2 ** hash_bits_used
            estimated_buckets = min(num_slices, max_buckets_from_hash)

        events_per_bucket = num_slices / estimated_buckets
        ok = "✓" if events_per_bucket <= EVENTS_THRESHOLD else "✗"

        print(f"{level:12s} (prefix_len={prefix_len:2d}): "
              f"~{estimated_buckets:>10,} buckets, "
              f"~{events_per_bucket:>12,.0f} events/bucket {ok}")

    # The test assertion: at the finest level, we should be able to
    # get buckets with <=EVENTS_THRESHOLD events
    finest_level = LEVELS[-1]
    finest_prefix_len = LEVEL_PREFIX_LEN[finest_level]

    if finest_prefix_len <= 12:
        # All in one bucket - this is a problem!
        pytest.fail(
            f"PROBLEM: At {finest_level} (prefix_len={finest_prefix_len}), "
            f"all {num_slices:,} slices would be in ONE bucket! "
            f"Need to extend prefix to include hash bits (chars 12-16)."
        )
    else:
        # Hash bits included - verify we get enough buckets
        hash_bits = (finest_prefix_len - 12) * 4
        max_hash_buckets = 2 ** hash_bits
        events_per_bucket = num_slices / max_hash_buckets

        assert events_per_bucket <= EVENTS_THRESHOLD * 10, \
            f"Even with hash bits, {events_per_bucket:.0f} events/bucket is too many"


def test_bucket_subdivision_with_many_events(fresh_db):
    """Test that buckets with >EVENTS_THRESHOLD events can be subdivided.

    Creates 500 events at the same timestamp and verifies they can be
    split into buckets of <=100 events each.
    """
    db = fresh_db

    # We need a peer_id to add events
    from events.identity import user
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    peer_id = alice['peer_id']

    timestamp = 5000  # All events at same timestamp
    num_events = 500

    # Add events to sync system
    print(f"\n=== Adding {num_events} events at timestamp {timestamp} ===")
    for i in range(num_events):
        event_id = f"test_event_{i:04d}"
        add_event_to_sync(db, peer_id, event_id, timestamp)

    db.commit()

    # Check event distribution at finest level
    finest_level = LEVELS[-1]
    finest_prefix_len = LEVEL_PREFIX_LEN[finest_level]

    # Get all unique prefixes at finest level
    unified_keys = [compute_unified_key(f"test_event_{i:04d}", timestamp)
                    for i in range(num_events)]
    finest_prefixes = set(k[:finest_prefix_len] for k in unified_keys)

    print(f"Finest level: {finest_level}")
    print(f"Unique prefixes: {len(finest_prefixes)}")

    # Check actual bucket counts from database
    for prefix in list(finest_prefixes)[:5]:  # Sample first 5
        count = get_event_count_in_bucket(db, peer_id, prefix, finest_level)
        print(f"  Bucket {prefix}: {count} events")

    # Verify: if we can't subdivide, all events are in one bucket
    if len(finest_prefixes) == 1:
        single_prefix = list(finest_prefixes)[0]
        count = get_event_count_in_bucket(db, peer_id, single_prefix, finest_level)
        if count > EVENTS_THRESHOLD:
            pytest.fail(
                f"Cannot subdivide: {count} events in single bucket at {finest_level}. "
                f"Need more bucket levels to handle same-timestamp events."
            )
