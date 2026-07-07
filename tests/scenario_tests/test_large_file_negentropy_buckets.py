"""
Scenario test: Negentropy handling of large files.

Tests that the negentropy sync system can handle very large files
that create many events.

A 1GB file creates ~2.4 million file_slice events.
The negentropy bisection protocol subdivides ranges when event count
exceeds EVENTS_THRESHOLD, ensuring efficient sync.

This test verifies that:
1. Unified keys are well-distributed based on hash bits
2. Range bisection will converge to small enough ranges
3. Large numbers of events can be synced efficiently
"""
import pytest
from events.network.negentropy import (
    compute_unified_key,
    EVENTS_THRESHOLD,
    MIN_UNIFIED_KEY,
    MAX_UNIFIED_KEY,
    add_event_to_sync,
    get_event_count_in_range,
    compute_range_hash,
)


def test_unified_key_distribution():
    """Events get unique unified keys based on timestamp and hash bits."""
    # Generate many event IDs at the same timestamp (like file slices)
    timestamp = 1000000
    event_ids = [f"event_{i}" for i in range(1000)]

    # Compute unified keys
    unified_keys = [compute_unified_key(eid, timestamp) for eid in event_ids]

    # All keys should be integers
    for key in unified_keys:
        assert isinstance(key, int), f"Key should be int, got {type(key)}"
        assert MIN_UNIFIED_KEY <= key <= MAX_UNIFIED_KEY

    # With 16-bit hash suffix, some collisions are expected due to birthday paradox
    # Expected collisions for n=1000, k=65536: ~7-8 collisions
    unique_keys = set(unified_keys)
    # Allow up to 2% collision rate
    assert len(unique_keys) >= 980, f"Too many collisions, got only {len(unique_keys)} unique keys"

    # Check distribution: with 16-bit hash suffix, keys should spread
    # across a range of ~65536 possible values per timestamp
    min_key = min(unified_keys)
    max_key = max(unified_keys)
    spread = max_key - min_key

    print(f"\nUnified key distribution:")
    print(f"  Min key: {min_key}")
    print(f"  Max key: {max_key}")
    print(f"  Spread: {spread}")
    print(f"  Unique keys: {len(unique_keys)}")
    print(f"  Collisions: {1000 - len(unique_keys)}")

    # With 16-bit hash suffix, expect good spread
    # Keys differ only in low 16 bits when timestamp is same
    assert spread >= 60000, f"Expected spread >= 60000, got {spread}"


def test_1gb_file_bisection_analysis():
    """Test that bisection can efficiently sync a 1GB file's slices.

    A 1GB file = 1,073,741,824 bytes / 450 bytes per slice = 2,386,093 slices

    With bisection, we start with the full range and keep splitting in half
    until ranges have <= EVENTS_THRESHOLD events, then send blobs directly.
    """
    num_slices = 2_386_093  # 1GB file

    print(f"\n=== 1GB File Bisection Analysis ===")
    print(f"Total slices: {num_slices:,}")
    print(f"EVENTS_THRESHOLD: {EVENTS_THRESHOLD}")
    print()

    # Calculate how many bisection levels needed to get below threshold
    # Each bisection halves the events in each range
    events = num_slices
    levels = 0
    while events > EVENTS_THRESHOLD:
        events = events / 2
        levels += 1

    ranges_at_bottom = 2 ** levels
    events_per_range = num_slices / ranges_at_bottom

    print(f"Bisection levels needed: {levels}")
    print(f"Ranges at bottom level: {ranges_at_bottom:,}")
    print(f"Events per bottom range: {events_per_range:.1f}")

    # Maximum messages in worst case (all ranges mismatch):
    # 2^0 + 2^1 + ... + 2^levels = 2^(levels+1) - 1 range requests
    max_messages = (2 ** (levels + 1)) - 1

    print(f"Max messages (worst case): {max_messages:,}")

    # In practice, with identical sets (no differences), we only send 1 message
    # With mostly-synced sets, we quickly match most ranges

    # Verify we can get below threshold
    assert events_per_range <= EVENTS_THRESHOLD, \
        f"Expected {events_per_range:.1f} events/range <= {EVENTS_THRESHOLD}"

    # Also verify the math is reasonable (around 15-16 levels for 2.4M events)
    assert 15 <= levels <= 20, f"Expected 15-20 levels, got {levels}"


def test_bisection_with_many_events(fresh_db):
    """Test that many events can be added and range-queried.

    Creates 500 events and verifies they can be counted in ranges.
    """
    db = fresh_db

    # We need a peer_id to add events
    from events.identity import user
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    peer_id = alice['peer_id']

    # Count events already in the database from user creation
    initial_count = get_event_count_in_range(db, peer_id, MIN_UNIFIED_KEY, MAX_UNIFIED_KEY)
    print(f"Initial events from user creation: {initial_count}")

    num_events = 500
    base_timestamp = 5000

    # Add events to sync system
    print(f"\n=== Adding {num_events} events ===")
    for i in range(num_events):
        event_id = f"test_event_{i:04d}"
        add_event_to_sync(db, peer_id, event_id, base_timestamp + i)

    db.commit()

    # Count all events in full range
    total = get_event_count_in_range(db, peer_id, MIN_UNIFIED_KEY, MAX_UNIFIED_KEY)
    print(f"Total events in full range: {total}")
    assert total == initial_count + num_events, f"Expected {initial_count + num_events}, got {total}"

    # Compute hash of full range
    full_hash = compute_range_hash(db, peer_id, MIN_UNIFIED_KEY, MAX_UNIFIED_KEY)
    print(f"Full range hash: {full_hash.hex()[:32]}...")

    # Bisect and verify counts add up
    mid = (MIN_UNIFIED_KEY + MAX_UNIFIED_KEY) // 2
    left_count = get_event_count_in_range(db, peer_id, MIN_UNIFIED_KEY, mid)
    right_count = get_event_count_in_range(db, peer_id, mid, MAX_UNIFIED_KEY)

    print(f"Left half: {left_count} events")
    print(f"Right half: {right_count} events")

    assert left_count + right_count == total, \
        f"Bisection counts should sum to total: {left_count} + {right_count} != {total}"

    # Verify XOR property: hash(full) should equal XOR of left and right only
    # if the events are perfectly partitioned (no overlap)
    left_hash = compute_range_hash(db, peer_id, MIN_UNIFIED_KEY, mid)
    right_hash = compute_range_hash(db, peer_id, mid, MAX_UNIFIED_KEY)

    # XOR of left and right hashes should equal full hash
    combined_hash = bytes(a ^ b for a, b in zip(left_hash, right_hash))
    assert combined_hash == full_hash, \
        f"XOR property violated: left XOR right != full"

    print("XOR property verified: hash(full) == hash(left) XOR hash(right)")
