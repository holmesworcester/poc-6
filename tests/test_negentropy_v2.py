"""Tests for negentropy v2 binary bisection protocol."""
import pytest
from events.network import negentropy
from events.network.negentropy import (
    RANGE_MIN, RANGE_MAX, ZERO_HASH,
    range_to_hex, hex_to_range, range_midpoint,
    compute_range_hash, count_events_in_range, get_events_in_range,
    first_leaf_in_range, handle_v2_range, initiate_v2_sync,
)


class TestRangeHelpers:
    """Test range helper functions."""

    def test_range_to_hex_normal(self):
        """Normal values use 12 hex chars."""
        assert range_to_hex(0) == "000000000000"
        assert range_to_hex(255) == "0000000000ff"
        assert range_to_hex(0xffffffffffff) == "ffffffffffff"

    def test_range_to_hex_max(self):
        """Max+1 (exclusive end) uses 13 hex chars."""
        assert range_to_hex(RANGE_MAX) == "1000000000000"

    def test_hex_to_range(self):
        """Hex strings convert back to integers."""
        assert hex_to_range("000000000000") == 0
        assert hex_to_range("ffffffffffff") == 0xffffffffffff
        assert hex_to_range("1000000000000") == RANGE_MAX

    def test_range_midpoint(self):
        """Midpoint calculation."""
        assert range_midpoint(0, 100) == 50
        assert range_midpoint(0, RANGE_MAX) == RANGE_MAX // 2
        assert range_midpoint(100, 200) == 150


class TestRangeHash:
    """Test range hash computation."""

    def test_empty_range_returns_zero_hash(self, fresh_db):
        """Empty range returns ZERO_HASH."""
        db = fresh_db
        peer_id = "test_peer_123"

        result = compute_range_hash(db, peer_id, 0, RANGE_MAX)
        assert result == ZERO_HASH

    def test_range_with_events(self, fresh_db):
        """Range with events returns non-zero hash."""
        from tests.utils.tick_helper import TestClock
        db = fresh_db
        clock = TestClock()
        peer_id = "test_peer_123"

        # Add some events
        events = [
            (f"event_{i}", clock.tick())
            for i in range(10)
        ]
        negentropy.add_events_to_sync_batch(db, peer_id, events)
        db.commit()

        result = compute_range_hash(db, peer_id, 0, RANGE_MAX)
        assert result != ZERO_HASH

    def test_disjoint_ranges_independent(self, fresh_db):
        """Events in one range don't affect another."""
        from tests.utils.tick_helper import TestClock
        db = fresh_db
        clock = TestClock()
        peer_id = "test_peer_123"

        # Add events (will have unified_key based on timestamp)
        events = [(f"event_{i}", clock.tick()) for i in range(10)]
        negentropy.add_events_to_sync_batch(db, peer_id, events)
        db.commit()

        # Get the actual range where events live
        full_hash = compute_range_hash(db, peer_id, 0, RANGE_MAX)

        # Range [0, 1) should be empty (events are at much higher keys)
        tiny_hash = compute_range_hash(db, peer_id, 0, 1)
        assert tiny_hash == ZERO_HASH


class TestCountEventsInRange:
    """Test event counting in ranges."""

    def test_empty_range(self, fresh_db):
        """Empty range returns 0."""
        db = fresh_db
        peer_id = "test_peer_123"

        count = count_events_in_range(db, peer_id, 0, RANGE_MAX)
        assert count == 0

    def test_counts_events(self, fresh_db):
        """Counts events correctly."""
        from tests.utils.tick_helper import TestClock
        db = fresh_db
        clock = TestClock()
        peer_id = "test_peer_123"

        events = [(f"event_{i}", clock.tick()) for i in range(25)]
        negentropy.add_events_to_sync_batch(db, peer_id, events)
        db.commit()

        count = count_events_in_range(db, peer_id, 0, RANGE_MAX)
        assert count == 25


class TestFirstLeafInRange:
    """Test first leaf finding."""

    def test_empty_range_returns_full_range(self, fresh_db):
        """Empty range is already a leaf."""
        db = fresh_db
        peer_id = "test_peer_123"

        start, end = first_leaf_in_range(db, peer_id, 0, RANGE_MAX)
        assert start == 0
        assert end == RANGE_MAX

    def test_small_range_returns_full_range(self, fresh_db):
        """Range under threshold is already a leaf."""
        from tests.utils.tick_helper import TestClock
        db = fresh_db
        clock = TestClock()
        peer_id = "test_peer_123"

        # Add fewer events than threshold
        events = [(f"event_{i}", clock.tick()) for i in range(10)]
        negentropy.add_events_to_sync_batch(db, peer_id, events)
        db.commit()

        start, end = first_leaf_in_range(db, peer_id, 0, RANGE_MAX)
        # Should return full range since count <= threshold
        assert start == 0
        assert end == RANGE_MAX

    def test_large_range_bisects(self, fresh_db):
        """Large range bisects to find leaf."""
        from tests.utils.tick_helper import TestClock
        db = fresh_db
        clock = TestClock()
        peer_id = "test_peer_123"

        # Add more events than threshold
        events = [(f"event_{i}", clock.tick()) for i in range(200)]
        negentropy.add_events_to_sync_batch(db, peer_id, events)
        db.commit()

        start, end = first_leaf_in_range(db, peer_id, 0, RANGE_MAX)

        # Should have bisected
        assert end < RANGE_MAX or start > 0

        # Leaf should have <= threshold events
        count = count_events_in_range(db, peer_id, start, end)
        assert count <= negentropy.EVENTS_THRESHOLD


class TestV2Protocol:
    """Test v2 binary bisection protocol."""

    def test_initiate_sync(self, fresh_db):
        """Initiate sends root range hash."""
        db = fresh_db
        peer_id = "test_peer_123"
        conn_id = "conn_123"

        msgs = initiate_v2_sync(db, peer_id, conn_id, 1000)

        assert len(msgs) == 1
        assert msgs[0]['type'] == 'v2_range'
        assert msgs[0]['start'] == range_to_hex(RANGE_MIN)
        assert msgs[0]['end'] == range_to_hex(RANGE_MAX)
        assert msgs[0]['hash'] == ZERO_HASH.hex()  # Empty peer

    def test_matching_hashes_return_match(self, fresh_db):
        """Matching hashes return v2_match."""
        db = fresh_db
        peer_id = "test_peer_123"
        conn_id = "conn_123"

        # Both empty - hashes match
        msg = {
            'type': 'v2_range',
            'start': range_to_hex(0),
            'end': range_to_hex(RANGE_MAX),
            'hash': ZERO_HASH.hex(),
        }

        responses = handle_v2_range(db, peer_id, conn_id, msg, 1000)

        assert len(responses) == 1
        assert responses[0]['type'] == 'v2_match'

    def test_they_have_nothing_we_have_events(self, fresh_db):
        """When they have nothing, we send events."""
        from tests.utils.tick_helper import TestClock
        db = fresh_db
        clock = TestClock()
        peer_id = "test_peer_123"
        conn_id = "conn_123"

        # Add events to our side
        events = [(f"event_{i}", clock.tick()) for i in range(10)]
        negentropy.add_events_to_sync_batch(db, peer_id, events)
        db.commit()

        # They send ZERO hash
        msg = {
            'type': 'v2_range',
            'start': range_to_hex(0),
            'end': range_to_hex(RANGE_MAX),
            'hash': ZERO_HASH.hex(),
        }

        responses = handle_v2_range(db, peer_id, conn_id, msg, clock.now())

        # Should get leaf range + possibly next range
        assert len(responses) >= 1
        assert responses[0]['type'] == 'v2_range'
        assert responses[0]['hash'] != ZERO_HASH.hex()

    def test_we_have_nothing_they_have_events(self, fresh_db):
        """When we have nothing, we request events."""
        from tests.utils.tick_helper import TestClock
        db = fresh_db
        clock = TestClock()
        peer_id = "test_peer_123"
        conn_id = "conn_123"

        # We have nothing, they have non-zero hash
        their_hash = "abcd" + "00" * 14  # Non-zero 16-byte hash

        msg = {
            'type': 'v2_range',
            'start': range_to_hex(0),
            'end': range_to_hex(RANGE_MAX),
            'hash': their_hash,
        }

        responses = handle_v2_range(db, peer_id, conn_id, msg, clock.now())

        # Should request by sending ZERO
        assert len(responses) == 1
        assert responses[0]['type'] == 'v2_range'
        assert responses[0]['hash'] == ZERO_HASH.hex()

    def test_mismatch_bisects(self, fresh_db):
        """Mismatch with many events bisects."""
        from tests.utils.tick_helper import TestClock
        db = fresh_db
        clock = TestClock()
        peer_id = "test_peer_123"
        conn_id = "conn_123"

        # Add many events
        events = [(f"event_{i}", clock.tick()) for i in range(200)]
        negentropy.add_events_to_sync_batch(db, peer_id, events)
        db.commit()

        # They have different hash (not zero, not matching)
        their_hash = "ffff" + "00" * 14

        msg = {
            'type': 'v2_range',
            'start': range_to_hex(0),
            'end': range_to_hex(RANGE_MAX),
            'hash': their_hash,
        }

        responses = handle_v2_range(db, peer_id, conn_id, msg, clock.now())

        # Should bisect - two range responses
        assert len(responses) == 2
        assert responses[0]['type'] == 'v2_range'
        assert responses[1]['type'] == 'v2_range'

        # Ranges should be left and right halves
        mid = range_midpoint(0, RANGE_MAX)
        assert responses[0]['end'] == range_to_hex(mid)
        assert responses[1]['start'] == range_to_hex(mid)
