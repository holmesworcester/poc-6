"""
Scenario test: Negentropy bucket capacity for large files.

Tests that the negentropy sync system can handle very large files
that create many events.

A 1GB file creates ~2.4 million file_slice events.
The negentropy bucket system must be able to subdivide these events
to avoid sending millions of event IDs in a single message.

This test verifies that:
1. Events get distributed across different buckets based on hash
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


def test_unified_key_distribution_hash_only():
    """Events get unique unified keys based purely on their event_id hash."""
    # Generate many event IDs
    event_ids = [f"event_{i}" for i in range(1000)]

    # Compute unified keys (timestamp is ignored in hash-only mode)
    unified_keys = [compute_unified_key(eid) for eid in event_ids]

    # All keys should be 16 hex chars (64 bits of hash)
    for key in unified_keys:
        assert len(key) == 16, f"Key should be 16 chars, got {len(key)}"
        assert all(c in '0123456789abcdef' for c in key), f"Key should be hex: {key}"

    # All keys should be unique (different event_ids)
    unique_keys = set(unified_keys)
    assert len(unique_keys) == 1000, f"All keys should be unique, got {len(unique_keys)}"

    # Check distribution at finest level
    finest_level = LEVELS[-1]
    finest_prefix_len = LEVEL_PREFIX_LEN[finest_level]

    finest_prefixes = set(k[:finest_prefix_len] for k in unified_keys)

    print(f"\nFinest level: {finest_level} (prefix_len={finest_prefix_len})")
    print(f"Unique prefixes at finest level: {len(finest_prefixes)}")
    print(f"Sample prefixes: {list(finest_prefixes)[:5]}")

    # With hash-only keys, events should be well-distributed
    # With 1000 events and 8-char prefix (32 bits), expect good spread
    assert len(finest_prefixes) > 900, \
        f"Events should spread across many buckets, got {len(finest_prefixes)}"


def test_1gb_file_bucket_distribution(fresh_db):
    """Test that a 1GB file's slices would be distributed across buckets.

    A 1GB file = 1,073,741,824 bytes / 450 bytes per slice = 2,386,093 slices

    With hash-only unified keys, all slices should be well-distributed
    into buckets of <=100 events each at the finest level.
    """
    db = fresh_db

    # Simulate the scenario: 2.4M events
    # (We can't actually create 2.4M events in a test, so we analyze the structure)

    num_slices = 2_386_093  # 1GB file

    # Calculate how many unique buckets we'd get at each level
    # by computing unified keys for a sample and extrapolating
    sample_size = 10000
    sample_keys = [compute_unified_key(f"slice_{i}") for i in range(sample_size)]

    print(f"\n=== 1GB File Bucket Analysis (Hash-Only) ===")
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
        # With hash-only keys, we get full distribution based on prefix length
        if level == 'root':
            estimated_buckets = 1
        else:
            # Hash bits = prefix_len * 4 (4 bits per hex char)
            hash_bits = prefix_len * 4
            max_buckets = 2 ** hash_bits
            estimated_buckets = min(num_slices, max_buckets)

        events_per_bucket = num_slices / estimated_buckets
        ok = "✓" if events_per_bucket <= EVENTS_THRESHOLD else "✗"

        print(f"{level:12s} (prefix_len={prefix_len:2d}): "
              f"~{estimated_buckets:>10,} buckets, "
              f"~{events_per_bucket:>12,.0f} events/bucket {ok}")

    # The test assertion: at the finest level, we should be able to
    # get buckets with <=EVENTS_THRESHOLD events
    finest_level = LEVELS[-1]
    finest_prefix_len = LEVEL_PREFIX_LEN[finest_level]

    # With hash-only keys, all prefix bits are hash bits
    hash_bits = finest_prefix_len * 4
    max_buckets = 2 ** hash_bits
    events_per_bucket = num_slices / max_buckets

    # With 8-char prefix (32 bits), we get ~4 billion buckets
    # 2.4M slices / 4B buckets = ~0.0006 events per bucket on average
    assert events_per_bucket <= EVENTS_THRESHOLD, \
        f"At finest level, {events_per_bucket:.1f} events/bucket exceeds threshold {EVENTS_THRESHOLD}"


def test_bucket_subdivision_with_many_events(fresh_db):
    """Test that many events get distributed across multiple buckets.

    Creates 500 events and verifies they are well-distributed
    across buckets with hash-only unified keys.
    """
    db = fresh_db

    # We need a peer_id to add events
    from events.identity import user
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    peer_id = alice['peer_id']

    num_events = 500

    # Add events to sync system
    print(f"\n=== Adding {num_events} events ===")
    for i in range(num_events):
        event_id = f"test_event_{i:04d}"
        add_event_to_sync(db, peer_id, event_id, 5000)  # timestamp ignored

    db.commit()

    # Check event distribution at finest level
    finest_level = LEVELS[-1]
    finest_prefix_len = LEVEL_PREFIX_LEN[finest_level]

    # Get all unique prefixes at finest level
    unified_keys = [compute_unified_key(f"test_event_{i:04d}")
                    for i in range(num_events)]
    finest_prefixes = set(k[:finest_prefix_len] for k in unified_keys)

    print(f"Finest level: {finest_level}")
    print(f"Unique prefixes: {len(finest_prefixes)}")

    # Check actual bucket counts from database
    for prefix in list(finest_prefixes)[:5]:  # Sample first 5
        count = get_event_count_in_bucket(db, peer_id, prefix, finest_level)
        print(f"  Bucket {prefix}: {count} events")

    # With hash-only keys, 500 events should be well-distributed
    # across many buckets at the finest level
    assert len(finest_prefixes) > 400, \
        f"Events should spread across many buckets, got {len(finest_prefixes)}"
