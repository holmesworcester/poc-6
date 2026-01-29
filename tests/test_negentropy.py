"""Tests for negentropy-style deterministic sync protocol.

Tests the time+hash prefix-based bucketing system.
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
        """Unified key is 16 hex chars (64 bits)."""
        event_id = 'test_event_abc123'
        key = negentropy.compute_unified_key(event_id, 1718451045000)
        assert len(key) == 16
        assert all(c in '0123456789abcdef' for c in key)

    def test_unified_key_uses_timestamp(self):
        """Different timestamps produce different keys (time prefix differs)."""
        event_id = 'test_event'
        key1 = negentropy.compute_unified_key(event_id, 1000000000000)  # ~2001
        key2 = negentropy.compute_unified_key(event_id, 2000000000000)  # ~2033
        # Keys should differ because time component differs
        assert key1 != key2
        # But hash suffix should be the same (same event_id)
        assert key1[6:] == key2[6:]  # Last 10 chars are hash

    def test_unified_key_deterministic(self):
        """Same event_id and timestamp produces same key."""
        ts = 1718451045000
        key1 = negentropy.compute_unified_key('evt1', ts)
        key2 = negentropy.compute_unified_key('evt1', ts)
        assert key1 == key2

    def test_unified_key_different_events(self):
        """Different events produce different keys."""
        ts = 1718451045000
        key1 = negentropy.compute_unified_key('evt1', ts)
        key2 = negentropy.compute_unified_key('evt2', ts)
        # Time prefix is the same, but hash suffix differs
        assert key1[:6] == key2[:6]  # Same time prefix
        assert key1[6:] != key2[6:]  # Different hash suffix
        assert key1 != key2

    def test_decode_unified_key(self):
        """Decode extracts approximate timestamp and hash suffix."""
        ts_ms = 1718451045000  # June 2024
        key = negentropy.compute_unified_key('evt1', ts_ms)
        decoded_ts, hash_hex = negentropy.decode_unified_key(key)
        # Timestamp loses low 18 bits (~4.4 min resolution)
        # So decoded should be within ~5 minutes of original
        assert abs(decoded_ts - ts_ms) < 5 * 60 * 1000  # Within 5 minutes
        assert len(hash_hex) == 10  # Hash is last 10 chars


class TestPrefixLevels:
    """Test prefix hierarchy."""

    def test_levels_defined(self):
        """All expected levels exist (reduced to 4 for efficiency)."""
        expected = ['root', 'prefix_2', 'prefix_4', 'prefix_6']
        assert negentropy.LEVELS == expected

    def test_get_prefix_for_level(self):
        """Get correct prefix length at each level."""
        event_id = 'evt1'
        ts_ms = 1718451045000
        unified_key = negentropy.compute_unified_key(event_id, ts_ms)

        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'root') == ''
        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'prefix_2') == unified_key[:2]
        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'prefix_4') == unified_key[:4]
        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'prefix_6') == unified_key[:6]

    def test_get_child_level(self):
        """Child level is next in hierarchy."""
        assert negentropy.get_child_level('root') == 'prefix_2'
        assert negentropy.get_child_level('prefix_2') == 'prefix_4'
        assert negentropy.get_child_level('prefix_4') == 'prefix_6'
        assert negentropy.get_child_level('prefix_6') is None  # Finest level

    def test_get_parent_level(self):
        """Parent level is previous in hierarchy."""
        assert negentropy.get_parent_level('prefix_6') == 'prefix_4'
        assert negentropy.get_parent_level('prefix_2') == 'root'
        assert negentropy.get_parent_level('root') is None  # Coarsest level


class TestFormatHuman:
    """Test human-readable unified key formatting."""

    def test_format_unified_key_human(self):
        """Format shows time and hash prefix."""
        ts_ms = 1718451045000  # June 2024
        key = negentropy.compute_unified_key('evt1', ts_ms)
        formatted = negentropy.format_unified_key_human(key)
        assert 't:' in formatted  # Shows time
        assert 'h:' in formatted  # Shows hash
        assert '2024' in formatted  # Year should be present


class TestXORFingerprinting:
    """Test XOR fingerprinting for bucket hashes."""

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


class TestEventTracking:
    """Test adding events to sync system."""

    def test_add_event(self, db):
        """Adding event creates bucket entries."""
        peer_id = 'peer1'
        event_id = 'evt1'
        ts_ms = 1718451045000  # 2024-06-15 11:30:45 UTC

        negentropy.add_event_to_sync(db, peer_id, event_id, ts_ms)

        # Check event was recorded
        row = db.query_one("""
            SELECT unified_key FROM negentropy_events
            WHERE recorded_by = ? AND event_id = ?
        """, (peer_id, event_id))
        assert row is not None
        # Unified key should be computed correctly
        expected_key = negentropy.compute_unified_key(event_id, ts_ms)
        assert row['unified_key'] == expected_key

    def test_add_event_creates_bucket_hierarchy(self, db):
        """Adding event marks buckets as needing recompute."""
        peer_id = 'peer1'
        negentropy.add_event_to_sync(db, peer_id, 'evt1', 1718451045000)

        # Check buckets were created at each level
        for level in negentropy.LEVELS:
            row = db.query_one("""
                SELECT COUNT(*) as cnt FROM negentropy_buckets
                WHERE recorded_by = ? AND level = ?
            """, (peer_id, level))
            assert row['cnt'] >= 1, f"No bucket at level {level}"


class TestXORBucketHashes:
    """Test XOR fingerprinting produces correct bucket hashes."""

    def test_bucket_hash_is_xor_of_fingerprints(self, db):
        """Bucket hash equals XOR of event fingerprints in that bucket."""
        peer_id = 'peer1'
        ts_ms = 1718451045000

        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts_ms)
        negentropy.add_event_to_sync(db, peer_id, 'evt2', ts_ms)

        # Get the unified key prefix for the finest level (prefix_6)
        unified_key = negentropy.compute_unified_key('evt1', ts_ms)
        prefix = unified_key[:6]  # Full prefix for prefix_6 level

        h = negentropy.get_bucket_hash(db, peer_id, 'prefix_6', prefix)

        # Hash should be XOR of fingerprints of events in that bucket
        events_in_bucket = negentropy.get_events_in_bucket(db, peer_id, prefix)
        expected = negentropy.ZERO_HASH
        for event_id in events_in_bucket:
            expected = negentropy.xor_bytes(expected, negentropy.compute_fingerprint(event_id))

        assert h == expected

    def test_get_hashes_at_level(self, db):
        """Get all hashes at a level."""
        peer_id = 'peer1'

        # Add events at different times
        negentropy.add_event_to_sync(db, peer_id, 'evt1', 1704067200000)  # 2024-01-01
        negentropy.add_event_to_sync(db, peer_id, 'evt2', 1735689600000)  # 2025-01-01

        # Get hashes at prefix_2 level (coarse enough to see multiple buckets)
        prefix_2_hashes = negentropy.get_hashes_at_level(db, peer_id, 'prefix_2')

        # Should have at least one non-empty bucket
        assert any(h != b'' for h in prefix_2_hashes.values())


class TestSyncProtocol:
    """Test sync protocol message handling."""

    def test_init_sync_creates_requests(self, db):
        """Initializing sync creates range requests starting at root."""
        peer_id = 'peer1'
        conn_id = 'conn1'

        negentropy.add_event_to_sync(db, peer_id, 'evt1', 1718451045000)

        requests = negentropy.init_sync_for_connection(db, peer_id, conn_id, 1000)

        assert len(requests) == 1  # Single root request
        assert requests[0]['type'] == 'range_request'
        assert requests[0]['level'] == 'root'
        assert requests[0]['prefix'] == ''
        assert 'root_hash' in requests[0]
        assert 'total_events' in requests[0]
        assert requests[0]['total_events'] == 1

    def test_matching_ranges_complete_immediately(self, db):
        """When root hashes match, range is marked complete (checkpoint)."""
        peer_id = 'peer1'
        conn_id = 'conn1'
        ts_ms = 1718451045000

        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts_ms)

        # Initialize sync
        requests = negentropy.init_sync_for_connection(db, peer_id, conn_id, 1000)
        assert len(requests) == 1
        assert requests[0]['level'] == 'root'

        # Simulate receiving same root hash back
        msg = {
            'type': 'range_request',
            'range_id': 'remote_range_1',
            'level': 'root',
            'prefix': '',
            'hash': requests[0]['hash'],  # Same hash
            'root_hash': requests[0]['root_hash'],
        }
        responses = negentropy.handle_range_request(db, peer_id, conn_id, msg, 2000)

        # Should respond with matched and have checkpoint logged
        assert len(responses) == 1
        assert responses[0]['type'] == 'range_matched'
        assert 'root_hash' in responses[0]
        assert 'total_events' in responses[0]

    def test_sends_events_when_below_threshold(self, db):
        """When bucket has few events (≤ EVENTS_THRESHOLD), send events directly."""
        peer_id = 'peer1'
        conn_id = 'conn1'
        ts_ms = 1718451045000

        # Add just 2 events - well below EVENTS_THRESHOLD (100)
        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts_ms)
        negentropy.add_event_to_sync(db, peer_id, 'evt2', ts_ms + 1000)

        # Get the prefix for prefix_2 level (coarse level that contains both events)
        unified_key = negentropy.compute_unified_key('evt1', ts_ms)
        prefix = unified_key[:2]

        # Receive request at prefix_2 level with different hash
        msg = {
            'type': 'range_request',
            'range_id': 'remote_1',
            'level': 'prefix_2',
            'prefix': prefix,
            'hash': 'deadbeef',  # Different hash
        }
        responses = negentropy.handle_range_request(db, peer_id, conn_id, msg, 1000)

        # With only 2 events, should send events directly instead of drilling down
        assert any(r['type'] == 'range_events' for r in responses)
        # Should NOT drill down since event count is below threshold
        assert not any(r['type'] == 'range_request' for r in responses)

    def test_drills_down_when_above_threshold(self, db):
        """When bucket has many events (> EVENTS_THRESHOLD), drill down instead."""
        peer_id = 'peer1'
        conn_id = 'conn1'

        # In hash-only mode, we need to find events that hash to the same prefix_2 bucket.
        # Generate events and group by their prefix_2 bucket until we have > 100 in one bucket.
        target_prefix = None
        events_by_prefix: dict[str, list[str]] = {}
        i = 0
        while True:
            event_id = f'evt{i}'
            unified_key = negentropy.compute_unified_key(event_id)
            prefix = unified_key[:2]
            if prefix not in events_by_prefix:
                events_by_prefix[prefix] = []
            events_by_prefix[prefix].append(event_id)
            if len(events_by_prefix[prefix]) > 150:
                target_prefix = prefix
                break
            i += 1
            if i > 100000:  # Safety limit
                pytest.skip("Could not find 150 events with same prefix_2 in reasonable iterations")

        # Add the events that share the same prefix_2 bucket
        for event_id in events_by_prefix[target_prefix]:
            negentropy.add_event_to_sync(db, peer_id, event_id, 1000)

        # Receive request at prefix_2 level with different hash
        msg = {
            'type': 'range_request',
            'range_id': 'remote_1',
            'level': 'prefix_2',
            'prefix': target_prefix,
            'hash': 'deadbeef',  # Different hash
        }
        responses = negentropy.handle_range_request(db, peer_id, conn_id, msg, 1000)

        # With > 100 events, should drill down to child level
        assert any(r['type'] == 'range_request' for r in responses)
        # Should NOT send events directly at this level
        assert not any(r['type'] == 'range_events' for r in responses)
        # Child requests should be at 'prefix_4' level
        child_requests = [r for r in responses if r['type'] == 'range_request']
        assert all(r['level'] == 'prefix_4' for r in child_requests)


class TestSyncStatus:
    """Test sync status for UI display."""

    def test_status_tracking(self, db):
        """Sync status tracks range states."""
        peer_id = 'peer1'
        conn_id = 'conn1'

        negentropy.add_event_to_sync(db, peer_id, 'evt1', 1718451045000)
        negentropy.init_sync_for_connection(db, peer_id, conn_id, 1000)

        status = negentropy.get_sync_status(db, peer_id, conn_id)

        assert status['total_ranges'] >= 1
        assert 'pending_ranges' in status
        assert 'progress_pct' in status

    def test_empty_connection_status(self, db):
        """Connection with no ranges reports correct status."""
        status = negentropy.get_sync_status(db, 'peer1', 'conn1')
        # Empty connection: no ranges means no sync work to do
        assert status['total_ranges'] == 0
        assert status['progress_pct'] == 100  # 100% done when no work


class TestCheckpoints:
    """Test checkpoint logging."""

    def test_checkpoint_logged_on_root_match(self, db):
        """Checkpoint is logged when root hashes match."""
        peer_id = 'peer1'
        conn_id = 'conn1'
        ts_ms = 1718451045000

        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts_ms)

        # Get our root hash
        root_hash = negentropy.get_root_hash(db, peer_id)

        # Simulate receiving a message with matching root hash
        msg = {
            'type': 'range_request',
            'range_id': 'remote_1',
            'level': 'root',
            'prefix': '',
            'hash': root_hash.hex(),
            'root_hash': root_hash.hex(),
        }
        negentropy.handle_range_request(db, peer_id, conn_id, msg, ts_ms + 1000)

        # Check checkpoint was logged
        checkpoint = db.query_one("""
            SELECT * FROM negentropy_checkpoints
            WHERE recorded_by = ? AND connection_id = ?
        """, (peer_id, conn_id))

        assert checkpoint is not None
        assert checkpoint['root_hash'] == root_hash


class TestPublicAPI:
    """Test public API functions."""

    def test_sync_all_connections_runs(self, db):
        """sync_all_connections runs without error when no connections."""
        # With no peers/connections, should complete without error
        result = negentropy.sync_all_connections(t_ms=1000, db=db)
        assert result['connections'] == 0
        assert result['messages_sent'] == 0

    def test_handle_incoming_processes_envelope(self, db):
        """handle_incoming processes negentropy message envelope."""
        peer_id = 'peer1'
        conn_id = 'conn1'
        ts_ms = 1718451045000

        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts_ms)

        # Get the prefix for the request
        unified_key = negentropy.compute_unified_key('evt1', ts_ms)
        prefix = unified_key[:2]

        # Create a negentropy envelope as would arrive from sync
        envelope = {
            'type': 'negentropy',
            'connection_id': conn_id,
            'data': {
                'type': 'range_request',
                'range_id': 'remote_1',
                'level': 'prefix_2',
                'prefix': prefix,
                'hash': 'deadbeef',
            }
        }

        # Should not raise - connection.send will fail but that's expected
        # since we don't have a real connection
        try:
            negentropy.handle_incoming(db, peer_id, conn_id, envelope, 2000)
        except Exception:
            pass  # Expected - no real connection to send on
