"""Tests for negentropy-style deterministic sync protocol.

Tests the unified time+hash bisection-based sync system.
"""
import pytest
import sqlite3
from core.db import Database
from core import schema
from events.network import negentropy


@pytest.fixture
def db():
    """Create a fresh in-memory database with negentropy tables."""
    conn = sqlite3.Connection(":memory:")
    database = Database(conn)
    schema.create_all(database)  # Loads all .sql files including negentropy.sql
    return database


class TestUnifiedKey:
    """Test unified key computation (time+hash variant)."""

    def test_unified_key_format(self):
        """Unified key is 64-bit integer."""
        event_id = 'test_event_abc123'
        ts_ms = 1718451045000
        key = negentropy.compute_unified_key(event_id, ts_ms)
        assert isinstance(key, int)
        assert 0 <= key <= negentropy.MAX_UNIFIED_KEY

    def test_unified_key_uses_timestamp(self):
        """Different timestamps produce different keys (in high bits)."""
        event_id = 'test_event'
        key1 = negentropy.compute_unified_key(event_id, 1000000000000)
        key2 = negentropy.compute_unified_key(event_id, 2000000000000)
        assert key1 != key2
        # Higher timestamp should give higher key
        assert key2 > key1

    def test_unified_key_deterministic(self):
        """Same event_id and timestamp produces same key."""
        key1 = negentropy.compute_unified_key('evt1', 1000)
        key2 = negentropy.compute_unified_key('evt1', 1000)
        assert key1 == key2

    def test_unified_key_different_events_same_time(self):
        """Different events at same timestamp produce different keys (hash differs)."""
        ts = 1718451045000
        key1 = negentropy.compute_unified_key('evt1', ts)
        key2 = negentropy.compute_unified_key('evt2', ts)
        assert key1 != key2
        # High 48 bits (timestamp) should be same
        assert (key1 >> 16) == (key2 >> 16)
        # Low 16 bits (hash) should differ
        assert (key1 & 0xFFFF) != (key2 & 0xFFFF)

    def test_unified_key_ordering(self):
        """Events are ordered by time first, then hash."""
        early = negentropy.compute_unified_key('evt1', 1000)
        late = negentropy.compute_unified_key('evt2', 2000)
        assert early < late  # Earlier time = smaller key


class TestXORFingerprinting:
    """Test XOR fingerprinting for range hashes."""

    def test_fingerprint_is_16_bytes(self):
        """Fingerprint is always 16 bytes."""
        fp = negentropy.compute_fingerprint('abc123')
        assert len(fp) == 16

    def test_fingerprint_deterministic(self):
        """Same event_id produces same fingerprint."""
        fp1 = negentropy.compute_fingerprint('evt1')
        fp2 = negentropy.compute_fingerprint('evt1')
        assert fp1 == fp2

    def test_fingerprint_different_for_different_events(self):
        """Different events produce different fingerprints."""
        fp1 = negentropy.compute_fingerprint('evt1')
        fp2 = negentropy.compute_fingerprint('evt2')
        assert fp1 != fp2

    def test_xor_bytes(self):
        """XOR bytes works correctly."""
        a = b'\x01\x02\x03\x04'
        b = b'\x10\x20\x30\x40'
        result = negentropy.xor_bytes(a, b)
        assert result == b'\x11\x22\x33\x44'

    def test_xor_self_inverse(self):
        """XORing a value twice returns to original."""
        original = b'\xaa\xbb\xcc\xdd' * 4
        fp = negentropy.compute_fingerprint('test')
        xored = negentropy.xor_bytes(original, fp)
        restored = negentropy.xor_bytes(xored, fp)
        assert restored == original

    def test_xor_commutative(self):
        """XOR order doesn't matter (a^b^c = c^a^b)."""
        fp1 = negentropy.compute_fingerprint('evt1')
        fp2 = negentropy.compute_fingerprint('evt2')
        fp3 = negentropy.compute_fingerprint('evt3')

        # Order 1: fp1 ^ fp2 ^ fp3
        result1 = negentropy.xor_bytes(negentropy.xor_bytes(fp1, fp2), fp3)
        # Order 2: fp3 ^ fp1 ^ fp2
        result2 = negentropy.xor_bytes(negentropy.xor_bytes(fp3, fp1), fp2)
        # Order 3: fp2 ^ fp3 ^ fp1
        result3 = negentropy.xor_bytes(negentropy.xor_bytes(fp2, fp3), fp1)

        assert result1 == result2 == result3


class TestRangeHash:
    """Test range hash computation."""

    def test_empty_range_hash(self, db):
        """Empty range has zero hash."""
        peer_id = 'peer1'
        hash_val = negentropy.compute_range_hash(
            db, peer_id, negentropy.MIN_UNIFIED_KEY, negentropy.MAX_UNIFIED_KEY
        )
        assert hash_val == negentropy.ZERO_HASH

    def test_range_hash_single_event(self, db):
        """Range with single event has that event's fingerprint."""
        peer_id = 'peer1'
        event_id = 'evt1'
        ts_ms = 1718451045000

        negentropy.add_event_to_sync(db, peer_id, event_id, ts_ms)

        hash_val = negentropy.compute_range_hash(
            db, peer_id, negentropy.MIN_UNIFIED_KEY, negentropy.MAX_UNIFIED_KEY
        )
        expected = negentropy.compute_fingerprint(event_id)
        assert hash_val == expected

    def test_range_hash_multiple_events(self, db):
        """Range hash is XOR of all event fingerprints."""
        peer_id = 'peer1'
        ts_ms = 1718451045000

        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts_ms)
        negentropy.add_event_to_sync(db, peer_id, 'evt2', ts_ms + 1000)
        negentropy.add_event_to_sync(db, peer_id, 'evt3', ts_ms + 2000)

        hash_val = negentropy.compute_range_hash(
            db, peer_id, negentropy.MIN_UNIFIED_KEY, negentropy.MAX_UNIFIED_KEY
        )

        # Compute expected XOR
        expected = negentropy.compute_fingerprint('evt1')
        expected = negentropy.xor_bytes(expected, negentropy.compute_fingerprint('evt2'))
        expected = negentropy.xor_bytes(expected, negentropy.compute_fingerprint('evt3'))

        assert hash_val == expected

    def test_range_hash_partial_range(self, db):
        """Range hash only includes events in range."""
        peer_id = 'peer1'

        # Add events at different times
        negentropy.add_event_to_sync(db, peer_id, 'evt1', 1000)
        negentropy.add_event_to_sync(db, peer_id, 'evt2', 2000)
        negentropy.add_event_to_sync(db, peer_id, 'evt3', 3000)

        # Get unified keys to determine range
        key1 = negentropy.compute_unified_key('evt1', 1000)
        key2 = negentropy.compute_unified_key('evt2', 2000)
        key3 = negentropy.compute_unified_key('evt3', 3000)

        # Hash of just evt2 should match fingerprint of evt2
        hash_val = negentropy.compute_range_hash(db, peer_id, key2, key3)
        expected = negentropy.compute_fingerprint('evt2')
        assert hash_val == expected


class TestEventTracking:
    """Test event addition and tracking."""

    def test_add_event(self, db):
        """Adding event stores it in negentropy_events."""
        peer_id = 'peer1'
        event_id = 'evt1'
        ts_ms = 1718451045000

        negentropy.add_event_to_sync(db, peer_id, event_id, ts_ms)

        # Check it's stored
        row = db.query_one(
            "SELECT unified_key, created_at FROM negentropy_events WHERE event_id = ?",
            (event_id,)
        )
        assert row is not None
        assert row['created_at'] == ts_ms
        assert isinstance(row['unified_key'], int)

    def test_add_event_batch(self, db):
        """Batch add stores multiple events."""
        peer_id = 'peer1'
        events = [
            ('evt1', 1000),
            ('evt2', 2000),
            ('evt3', 3000),
        ]

        negentropy.add_events_to_sync_batch(db, peer_id, events)

        count = db.query_one(
            "SELECT COUNT(*) as cnt FROM negentropy_events WHERE recorded_by = ?",
            (peer_id,)
        )['cnt']
        assert count == 3

    def test_add_event_deduplication(self, db):
        """Adding same event twice doesn't create duplicate."""
        peer_id = 'peer1'
        event_id = 'evt1'
        ts_ms = 1000

        negentropy.add_event_to_sync(db, peer_id, event_id, ts_ms)
        negentropy.add_event_to_sync(db, peer_id, event_id, ts_ms)

        count = db.query_one(
            "SELECT COUNT(*) as cnt FROM negentropy_events WHERE event_id = ?",
            (event_id,)
        )['cnt']
        assert count == 1


class TestSyncProtocol:
    """Test the sync protocol messages."""

    def test_init_sync_creates_request(self, db):
        """Initializing sync creates a range_request for full range."""
        peer_id = 'peer1'
        conn_id = 'conn1'

        negentropy.add_event_to_sync(db, peer_id, 'evt1', 1000)

        requests = negentropy.init_sync_for_connection(db, peer_id, conn_id, 1000)

        assert len(requests) == 1
        req = requests[0]
        assert req['type'] == 'range_request'
        assert req['start'] == negentropy.MIN_UNIFIED_KEY
        assert req['end'] == negentropy.MAX_UNIFIED_KEY
        assert 'hash' in req
        assert 'root_hash' in req

    def test_init_sync_empty_still_sends_request(self, db):
        """Even with no events, we send a request to pull from peer."""
        peer_id = 'peer1'
        conn_id = 'conn1'

        requests = negentropy.init_sync_for_connection(db, peer_id, conn_id, 1000)
        # We still send a request even with no events - we might need to pull from peer
        assert len(requests) == 1
        req = requests[0]
        assert req['type'] == 'range_request'
        assert req['hash'] == negentropy.ZERO_HASH.hex()  # Empty hash

    def test_matching_hashes_return_matched(self, db):
        """When hashes match, return range_matched."""
        peer_id = 'peer1'
        conn_id = 'conn1'

        negentropy.add_event_to_sync(db, peer_id, 'evt1', 1000)

        # Get our hash
        our_hash = negentropy.get_root_hash(db, peer_id)

        msg = {
            'type': 'range_request',
            'range_id': 'test_range',
            'start': negentropy.MIN_UNIFIED_KEY,
            'end': negentropy.MAX_UNIFIED_KEY,
            'hash': our_hash.hex(),
        }

        responses = negentropy.handle_range_request(db, peer_id, conn_id, msg, 1000)

        assert len(responses) == 1
        assert responses[0]['type'] == 'range_matched'

    def test_few_events_sends_blobs_and_matched(self, db):
        """When hashes differ but few events, send blobs and return matched."""
        peer_id = 'peer1'
        conn_id = 'conn1'

        # Add just a few events (below threshold)
        for i in range(5):
            negentropy.add_event_to_sync(db, peer_id, f'evt{i}', 1000 + i)

        msg = {
            'type': 'range_request',
            'range_id': 'test_range',
            'start': negentropy.MIN_UNIFIED_KEY,
            'end': negentropy.MAX_UNIFIED_KEY,
            'hash': 'deadbeef' * 4,  # Different hash
        }

        responses = negentropy.handle_range_request(db, peer_id, conn_id, msg, 1000)

        # Should return range_matched (blobs sent separately)
        assert len(responses) == 1
        assert responses[0]['type'] == 'range_matched'

    def test_many_events_returns_mismatched(self, db):
        """When hashes differ and many events, return range_mismatched."""
        peer_id = 'peer1'
        conn_id = 'conn1'

        # Add many events (above threshold)
        for i in range(100):
            negentropy.add_event_to_sync(db, peer_id, f'evt{i}', 1000 + i)

        msg = {
            'type': 'range_request',
            'range_id': 'test_range',
            'start': negentropy.MIN_UNIFIED_KEY,
            'end': negentropy.MAX_UNIFIED_KEY,
            'hash': 'deadbeef' * 4,  # Different hash
        }

        responses = negentropy.handle_range_request(db, peer_id, conn_id, msg, 1000)

        assert len(responses) == 1
        assert responses[0]['type'] == 'range_mismatched'
        assert 'event_count' in responses[0]
        assert responses[0]['event_count'] == 100


class TestBisection:
    """Test the bisection logic."""

    def test_handle_mismatched_bisects_range(self, db):
        """Receiving range_mismatched triggers bisection."""
        peer_id = 'peer1'
        conn_id = 'conn1'

        # Add events
        for i in range(10):
            negentropy.add_event_to_sync(db, peer_id, f'evt{i}', 1000 + i)

        # First, init sync to create the range
        requests = negentropy.init_sync_for_connection(db, peer_id, conn_id, 1000)
        assert len(requests) == 1
        original_range_id = requests[0]['range_id']

        # Simulate receiving range_mismatched
        msg = {
            'type': 'range_mismatched',
            'range_id': original_range_id,
            'event_count': 1000,
        }

        responses = negentropy.handle_range_mismatched(db, peer_id, conn_id, msg, 2000)

        # Should get two child range_requests
        assert len(responses) == 2
        assert all(r['type'] == 'range_request' for r in responses)

        # Children should cover the full range without overlap
        left = responses[0]
        right = responses[1]

        # Assuming left comes first
        if left['start'] > right['start']:
            left, right = right, left

        assert left['start'] == negentropy.MIN_UNIFIED_KEY
        assert left['end'] == right['start']  # No gap
        assert right['end'] == negentropy.MAX_UNIFIED_KEY


class TestSyncStatus:
    """Test sync status tracking."""

    def test_empty_connection_status(self, db):
        """Connection with no ranges has 100% progress."""
        status = negentropy.get_sync_status(db, 'peer1', 'conn1')
        assert status['total_ranges'] == 0
        assert status['progress_pct'] == 100


class TestCheckpoints:
    """Test checkpoint logging."""

    def test_checkpoint_logged_on_root_match(self, db):
        """Checkpoint is logged when root hashes match."""
        peer_id = 'peer1'
        conn_id = 'conn1'

        negentropy.add_event_to_sync(db, peer_id, 'evt1', 1000)
        root_hash = negentropy.get_root_hash(db, peer_id)

        # Send a request with matching root_hash
        msg = {
            'type': 'range_request',
            'range_id': 'test_range',
            'start': negentropy.MIN_UNIFIED_KEY,
            'end': negentropy.MAX_UNIFIED_KEY,
            'hash': root_hash.hex(),
            'root_hash': root_hash.hex(),
        }

        negentropy.handle_range_request(db, peer_id, conn_id, msg, 1000)

        # Check checkpoint was logged
        checkpoint = db.query_one(
            "SELECT * FROM negentropy_checkpoints WHERE connection_id = ?",
            (conn_id,)
        )
        assert checkpoint is not None
        assert checkpoint['root_hash'] == root_hash


class TestPublicAPI:
    """Test public API functions."""

    def test_sync_all_connections_runs(self, db):
        """sync_all_connections executes without error."""
        # Just verify it doesn't crash with empty state
        result = negentropy.sync_all_connections(t_ms=1000, db=db)
        assert isinstance(result, dict)

    def test_handle_sync_message_routes_correctly(self, db):
        """handle_sync_message routes to correct handler."""
        peer_id = 'peer1'
        conn_id = 'conn1'

        negentropy.add_event_to_sync(db, peer_id, 'evt1', 1000)
        root_hash = negentropy.get_root_hash(db, peer_id)

        # Test range_request routing
        msg = {
            'type': 'range_request',
            'range_id': 'test1',
            'start': negentropy.MIN_UNIFIED_KEY,
            'end': negentropy.MAX_UNIFIED_KEY,
            'hash': root_hash.hex(),
        }
        responses = negentropy.handle_sync_message(db, peer_id, conn_id, msg, 1000)
        assert len(responses) == 1
        assert responses[0]['type'] == 'range_matched'

        # Test range_matched routing (should return empty)
        msg = {
            'type': 'range_matched',
            'range_id': 'test1',
        }
        responses = negentropy.handle_sync_message(db, peer_id, conn_id, msg, 1000)
        assert responses == []

        # Test unknown message type
        msg = {'type': 'unknown'}
        responses = negentropy.handle_sync_message(db, peer_id, conn_id, msg, 1000)
        assert responses == []
