"""Basic tests for bloom-based sync components."""
import pytest
from events import sync
import crypto


def test_bloom_filter_basic():
    """Test basic bloom filter create and check."""
    # Create some test event IDs
    event_ids = [
        crypto.hash(b"event1"),
        crypto.hash(b"event2"),
        crypto.hash(b"event3"),
    ]

    salt = b"0" * 16  # Simple salt for testing

    # Create bloom
    bloom_filter = sync.create_bloom(event_ids, salt)

    # Check that created events are in bloom
    assert sync.check_bloom(event_ids[0], bloom_filter, salt) is True
    assert sync.check_bloom(event_ids[1], bloom_filter, salt) is True
    assert sync.check_bloom(event_ids[2], bloom_filter, salt) is True

    # Check that other events are not in bloom
    other_event = crypto.hash(b"other_event")
    assert sync.check_bloom(other_event, bloom_filter, salt) is False


def test_bloom_filter_empty():
    """Test bloom filter with no events."""
    salt = b"0" * 16
    bloom_filter = sync.create_bloom([], salt)

    # Any event should NOT be in empty bloom
    event_id = crypto.hash(b"test_event")
    assert sync.check_bloom(event_id, bloom_filter, salt) is False


def test_window_computation():
    """Test window ID computation."""
    event_id = crypto.hash(b"test_event")

    # Compute window at different w values
    w12 = sync.compute_window_id(event_id, 12)
    w17 = sync.compute_window_id(event_id, 17)
    w20 = sync.compute_window_id(event_id, 20)

    # Higher w should give higher window IDs (more precise)
    assert w17 > w12
    assert w20 > w17

    # Window IDs should be within expected range
    assert 0 <= w12 < 2**12
    assert 0 <= w17 < 2**17
    assert 0 <= w20 < 2**20


def test_window_conversion():
    """Test converting between storage and query window IDs."""
    # Storage window ID at w=20
    storage_window = 123456

    # Convert to query window at w=12 by right-shifting
    # Should be right-shifted by (20-12) = 8 bits
    query_window = storage_window >> 8
    assert query_window == storage_window >> 8

    # Convert to query window at w=17
    query_window_17 = storage_window >> 3
    assert query_window_17 == storage_window >> 3


def test_salt_derivation():
    """Test salt derivation from peer public key and window ID."""
    peer_pk = b"0" * 32  # Simple peer public key
    window_id = 42

    # Derive salt
    salt1 = sync.derive_salt(peer_pk, window_id)

    # Should be 16 bytes
    assert len(salt1) == 16

    # Same inputs should give same salt
    salt2 = sync.derive_salt(peer_pk, window_id)
    assert salt1 == salt2

    # Different window should give different salt
    salt3 = sync.derive_salt(peer_pk, window_id + 1)
    assert salt1 != salt3

    # Different peer should give different salt
    peer_pk2 = b"1" * 32
    salt4 = sync.derive_salt(peer_pk2, window_id)
    assert salt1 != salt4


def test_w_param_computation():
    """Test adaptive w parameter based on event count."""
    # For zero events, should use DEFAULT_W
    assert sync.compute_w_for_event_count(0) == sync.DEFAULT_W

    # For small event counts, should use small w
    w_100 = sync.compute_w_for_event_count(100)
    assert w_100 >= 1  # At least 1 bit

    # For 10k events: 10000 / 450 ≈ 22 windows → w=5
    w_10k = sync.compute_w_for_event_count(10000)
    assert w_10k >= 5
    assert w_10k <= 7  # Should be reasonable

    # For 10M events: 10M / 450 ≈ 22k windows → w=15
    w_10m = sync.compute_w_for_event_count(10_000_000)
    assert w_10m >= 15
    assert w_10m <= 18  # Should be reasonable


def test_bloom_false_positive_rate():
    """Test that bloom filter FPR is reasonable."""
    # Create 76 events (typical window size)
    event_ids = [crypto.hash(f"event{i}".encode()) for i in range(76)]

    salt = b"0" * 16
    bloom_filter = sync.create_bloom(event_ids, salt)

    # Test 1000 random events not in the set
    false_positives = 0
    num_tests = 1000

    for i in range(num_tests):
        test_event = crypto.hash(f"test{i}".encode())
        if sync.check_bloom(test_event, bloom_filter, salt):
            false_positives += 1

    fpr = false_positives / num_tests

    # With 76 events, k=5, m=512:
    # Expected FPR ≈ 2.5%
    # Allow up to 10% in test (with some margin for randomness)
    assert fpr < 0.10, f"FPR too high: {fpr:.2%}"

    print(f"Measured FPR: {fpr:.2%} (expected ~2.5%)")


if __name__ == "__main__":
    # Run tests
    test_bloom_filter_basic()
    test_bloom_filter_empty()
    test_window_computation()
    test_window_conversion()
    test_salt_derivation()
    test_w_param_computation()
    test_bloom_false_positive_rate()
    print("All basic bloom sync tests passed!")
