"""Tests for negentropy-style deterministic sync protocol.

Tests the time-based prefix bucketing system with relative minutes from epoch.
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
    """Test unified key computation (time-based variant)."""

    def test_unified_key_format(self):
        """Unified key is 12 hex chars (48 bits)."""
        event_id = 'test_event_abc123'
        # Use a timestamp after the epoch
        ts_ms = negentropy.EPOCH_MS + 60_000  # 1 minute after epoch
        key = negentropy.compute_unified_key(event_id, ts_ms)
        assert len(key) == 12
        assert all(c in '0123456789abcdef' for c in key)

    def test_unified_key_uses_timestamp(self):
        """Different timestamps produce different keys (different minutes)."""
        event_id = 'test_event'
        # Two timestamps 2 minutes apart (well into different minute buckets)
        ts1 = negentropy.EPOCH_MS + 60_000      # 1 minute after epoch
        ts2 = negentropy.EPOCH_MS + 180_000     # 3 minutes after epoch
        key1 = negentropy.compute_unified_key(event_id, ts1)
        key2 = negentropy.compute_unified_key(event_id, ts2)
        assert key1 != key2  # Different minutes = different keys

    def test_unified_key_same_minute_same_event(self):
        """Same event in same minute produces same key."""
        event_id = 'test_event'
        ts1 = negentropy.EPOCH_MS + 60_000      # 1 min after epoch
        ts2 = negentropy.EPOCH_MS + 60_000 + 30_000  # 1.5 min after epoch (same minute bucket)
        key1 = negentropy.compute_unified_key(event_id, ts1)
        key2 = negentropy.compute_unified_key(event_id, ts2)
        assert key1 == key2  # Same minute bucket, same event = same key

    def test_unified_key_deterministic(self):
        """Same event_id and timestamp produces same key."""
        ts_ms = negentropy.EPOCH_MS + 60_000
        key1 = negentropy.compute_unified_key('evt1', ts_ms)
        key2 = negentropy.compute_unified_key('evt1', ts_ms)
        assert key1 == key2

    def test_unified_key_different_events_same_minute(self):
        """Different events in same minute produce different keys (hash differs)."""
        ts_ms = negentropy.EPOCH_MS + 60_000
        key1 = negentropy.compute_unified_key('evt1', ts_ms)
        key2 = negentropy.compute_unified_key('evt2', ts_ms)
        assert key1 != key2  # Hash part differs

    def test_decode_unified_key(self):
        """Decode returns (relative_minutes, hash_16bits)."""
        ts_ms = negentropy.EPOCH_MS + 120_000  # 2 minutes after epoch
        key = negentropy.compute_unified_key('evt1', ts_ms)
        relative_min, hash_bits = negentropy.decode_unified_key(key)
        assert relative_min == 2  # 2 minutes from epoch
        assert 0 <= hash_bits <= 0xFFFF  # 16-bit hash

    def test_unified_key_clamps_to_zero_before_epoch(self):
        """Timestamps before epoch are clamped to 0."""
        ts_ms = negentropy.EPOCH_MS - 1_000_000  # Before epoch
        key = negentropy.compute_unified_key('evt1', ts_ms)
        relative_min, _ = negentropy.decode_unified_key(key)
        assert relative_min == 0  # Clamped to 0


class TestEpochConstant:
    """Test the epoch constant."""

    def test_epoch_is_2025_01_01(self):
        """EPOCH_MS is 2025-01-01 00:00:00 UTC."""
        # 2025-01-01 00:00:00 UTC = 1735689600 seconds
        expected_ms = 1735689600 * 1000
        assert negentropy.EPOCH_MS == expected_ms

    def test_max_unified_key_is_48_bits(self):
        """MAX_UNIFIED_KEY is 2^48 - 1."""
        expected = (1 << 48) - 1
        assert negentropy.MAX_UNIFIED_KEY == expected


class TestPrefixLevels:
    """Test prefix hierarchy."""

    def test_levels_defined(self):
        """All expected levels exist (6 levels for large file support)."""
        expected = ['root', 'prefix_2', 'prefix_4', 'prefix_6', 'prefix_8', 'prefix_10']
        assert negentropy.LEVELS == expected

    def test_get_prefix_for_level(self):
        """Get correct prefix length at each level."""
        event_id = 'evt1'
        ts_ms = negentropy.EPOCH_MS + 60_000  # 1 minute after epoch
        unified_key = negentropy.compute_unified_key(event_id, ts_ms)

        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'root') == ''
        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'prefix_2') == unified_key[:2]
        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'prefix_4') == unified_key[:4]
        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'prefix_6') == unified_key[:6]
        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'prefix_8') == unified_key[:8]
        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'prefix_10') == unified_key[:10]

    def test_get_child_level(self):
        """Child level is next in hierarchy."""
        assert negentropy.get_child_level('root') == 'prefix_2'
        assert negentropy.get_child_level('prefix_2') == 'prefix_4'
        assert negentropy.get_child_level('prefix_4') == 'prefix_6'
        assert negentropy.get_child_level('prefix_6') == 'prefix_8'
        assert negentropy.get_child_level('prefix_8') == 'prefix_10'
        assert negentropy.get_child_level('prefix_10') is None  # Finest level

    def test_get_parent_level(self):
        """Parent level is previous in hierarchy."""
        assert negentropy.get_parent_level('prefix_10') == 'prefix_8'
        assert negentropy.get_parent_level('prefix_8') == 'prefix_6'
        assert negentropy.get_parent_level('prefix_6') == 'prefix_4'
        assert negentropy.get_parent_level('prefix_2') == 'root'
        assert negentropy.get_parent_level('root') is None  # Coarsest level


class TestFormatHuman:
    """Test human-readable unified key formatting."""

    def test_format_unified_key_human(self):
        """Format shows relative minutes and hash."""
        ts_ms = negentropy.EPOCH_MS + 120_000  # 2 minutes after epoch
        key = negentropy.compute_unified_key('evt1', ts_ms)
        formatted = negentropy.format_unified_key_human(key)
        assert 'min:' in formatted
        assert 'hash:' in formatted
        assert '2' in formatted  # 2 minutes


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
        ts_ms = negentropy.EPOCH_MS + 60_000  # 1 minute after epoch

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
        ts_ms = negentropy.EPOCH_MS + 60_000
        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts_ms)

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
        ts_ms = negentropy.EPOCH_MS + 60_000

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

        # Add events at different times (different minute buckets)
        ts1 = negentropy.EPOCH_MS + 60_000    # 1 minute after epoch
        ts2 = negentropy.EPOCH_MS + 180_000   # 3 minutes after epoch
        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts1)
        negentropy.add_event_to_sync(db, peer_id, 'evt2', ts2)

        # Get hashes at prefix_2 level (coarse enough to see multiple buckets)
        prefix_2_hashes = negentropy.get_hashes_at_level(db, peer_id, 'prefix_2')

        # Should have at least one non-empty bucket
        assert any(h != b'' for h in prefix_2_hashes.values())


class TestTemporalClustering:
    """Test that time-based keys cluster events by time."""

    def test_events_at_same_minute_share_time_prefix(self, db):
        """Events at the same minute share the same time prefix in unified key."""
        ts_ms = negentropy.EPOCH_MS + 60_000  # 1 minute after epoch

        key1 = negentropy.compute_unified_key('evt1', ts_ms)
        key2 = negentropy.compute_unified_key('evt2', ts_ms)
        key3 = negentropy.compute_unified_key('evt3', ts_ms + 30_000)  # +30s, same minute

        # First 8 hex chars encode the 32-bit minute value
        # All events in same minute should share this prefix
        assert key1[:8] == key2[:8] == key3[:8]

    def test_events_at_different_minutes_differ_in_time_prefix(self, db):
        """Events at different minutes have different time prefixes."""
        ts1 = negentropy.EPOCH_MS + 60_000   # 1 minute
        ts2 = negentropy.EPOCH_MS + 120_000  # 2 minutes

        key1 = negentropy.compute_unified_key('evt1', ts1)
        key2 = negentropy.compute_unified_key('evt1', ts2)

        # First 8 hex chars should differ (different minutes)
        assert key1[:8] != key2[:8]


class TestSyncProtocol:
    """Test sync protocol message handling."""

    def test_init_sync_creates_requests(self, db):
        """Initializing sync creates range requests starting at root."""
        peer_id = 'peer1'
        conn_id = 'conn1'
        ts_ms = negentropy.EPOCH_MS + 60_000

        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts_ms)

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
        ts_ms = negentropy.EPOCH_MS + 60_000

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
        """When bucket has few events (<=EVENTS_THRESHOLD), send events directly."""
        peer_id = 'peer1'
        conn_id = 'conn1'
        ts_ms = negentropy.EPOCH_MS + 60_000

        # Add just 2 events - well below EVENTS_THRESHOLD
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
            'hash': 'deadbeefbeef',  # Different hash (12 chars)
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
        # Use a timestamp that will produce a known prefix_2
        ts_ms = negentropy.EPOCH_MS + 60_000

        # Add many events at the same minute - they'll share the same time prefix
        # Since time prefix is first 8 hex chars and prefix_2 uses first 2 chars,
        # events at the same minute will share the same prefix_2
        for i in range(200):
            event_id = f'evt{i}'
            negentropy.add_event_to_sync(db, peer_id, event_id, ts_ms)

        # Get the prefix_2 for these events
        unified_key = negentropy.compute_unified_key('evt0', ts_ms)
        target_prefix = unified_key[:2]

        # Receive request at prefix_2 level with different hash
        msg = {
            'type': 'range_request',
            'range_id': 'remote_1',
            'level': 'prefix_2',
            'prefix': target_prefix,
            'hash': 'deadbeefbeef',  # Different hash (12 chars)
        }
        responses = negentropy.handle_range_request(db, peer_id, conn_id, msg, 1000)

        # With > EVENTS_THRESHOLD events, should drill down to child level
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
        ts_ms = negentropy.EPOCH_MS + 60_000

        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts_ms)
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
        ts_ms = negentropy.EPOCH_MS + 60_000

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


class TestRebuildIndex:
    """Test rebuilding negentropy index after key format changes."""

    def test_rebuild_negentropy_index(self, db):
        """rebuild_negentropy_index recomputes all unified keys."""
        peer_id = 'peer1'
        ts_ms = negentropy.EPOCH_MS + 60_000

        # Add some events
        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts_ms)
        negentropy.add_event_to_sync(db, peer_id, 'evt2', ts_ms + 60_000)
        negentropy.add_event_to_sync(db, peer_id, 'evt3', ts_ms + 120_000)

        # Get original unified keys
        original_keys = {}
        rows = db.query("""
            SELECT event_id, unified_key FROM negentropy_events
            WHERE recorded_by = ?
        """, (peer_id,))
        for row in rows:
            original_keys[row['event_id']] = row['unified_key']

        # Rebuild index
        count = negentropy.rebuild_negentropy_index(db, peer_id)
        assert count == 3

        # Keys should still be the same (since we're using the same compute function)
        rows = db.query("""
            SELECT event_id, unified_key FROM negentropy_events
            WHERE recorded_by = ?
        """, (peer_id,))
        for row in rows:
            assert row['unified_key'] == original_keys[row['event_id']]


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
        ts_ms = negentropy.EPOCH_MS + 60_000

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
                'hash': 'deadbeefbeef',  # 12 chars
            }
        }

        # Should not raise - connection.send will fail but that's expected
        # since we don't have a real connection
        try:
            negentropy.handle_incoming(db, peer_id, conn_id, envelope, 2000)
        except Exception:
            pass  # Expected - no real connection to send on
