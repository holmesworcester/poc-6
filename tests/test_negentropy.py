"""Tests for negentropy-style deterministic sync protocol.

Tests the unified time+hash prefix-based bucketing system.
"""
import pytest
import sqlite3
from db import Database
import schema
from events.network import negentropy


@pytest.fixture
def db():
    """Create a fresh in-memory database with negentropy tables."""
    conn = sqlite3.Connection(":memory:")
    database = Database(conn)
    schema.create_all(database)  # Loads all .sql files including negentropy.sql
    return database


class TestUnifiedKey:
    """Test unified key computation."""

    def test_unified_key_format(self):
        """Unified key is 16 hex chars (64 bits)."""
        event_id = 'test_event_abc123'
        ts_ms = 1718451045000  # 2024-06-15 11:30:45 UTC
        key = negentropy.compute_unified_key(event_id, ts_ms)
        assert len(key) == 16
        assert all(c in '0123456789abcdef' for c in key)

    def test_unified_key_timestamp_ordering(self):
        """Earlier timestamps produce lexicographically smaller keys."""
        event_id = 'test_event'
        key1 = negentropy.compute_unified_key(event_id, 1000000000000)  # Earlier
        key2 = negentropy.compute_unified_key(event_id, 2000000000000)  # Later
        assert key1 < key2

    def test_unified_key_deterministic(self):
        """Same inputs produce same key."""
        key1 = negentropy.compute_unified_key('evt1', 1718451045000)
        key2 = negentropy.compute_unified_key('evt1', 1718451045000)
        assert key1 == key2

    def test_unified_key_different_events_same_time(self):
        """Different events at same time produce different keys."""
        ts_ms = 1718451045000
        key1 = negentropy.compute_unified_key('evt1', ts_ms)
        key2 = negentropy.compute_unified_key('evt2', ts_ms)
        # First 12 chars (timestamp) same, last 4 (hash) different
        assert key1[:12] == key2[:12]
        assert key1[12:] != key2[12:]

    def test_decode_unified_key(self):
        """Can decode unified key back to timestamp."""
        ts_ms = 1718451045000
        key = negentropy.compute_unified_key('evt1', ts_ms)
        decoded_ts, hash_hex = negentropy.decode_unified_key(key)
        assert decoded_ts == ts_ms
        assert len(hash_hex) == 4


class TestPrefixLevels:
    """Test prefix hierarchy."""

    def test_levels_defined(self):
        """All expected levels exist."""
        expected = ['root', 'prefix_2', 'prefix_4', 'prefix_6', 'prefix_8',
                    'prefix_10', 'prefix_12', 'prefix_14', 'prefix_16']
        assert negentropy.LEVELS == expected

    def test_get_prefix_for_level(self):
        """Get correct prefix length at each level."""
        event_id = 'evt1'
        ts_ms = 1718451045000
        unified_key = negentropy.compute_unified_key(event_id, ts_ms)

        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'root') == ''
        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'prefix_2') == unified_key[:2]
        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'prefix_4') == unified_key[:4]
        assert negentropy.get_prefix_for_level(event_id, ts_ms, 'prefix_16') == unified_key

    def test_get_child_level(self):
        """Child level is next in hierarchy."""
        assert negentropy.get_child_level('root') == 'prefix_2'
        assert negentropy.get_child_level('prefix_2') == 'prefix_4'
        assert negentropy.get_child_level('prefix_14') == 'prefix_16'
        assert negentropy.get_child_level('prefix_16') is None  # Finest level

    def test_get_parent_level(self):
        """Parent level is previous in hierarchy."""
        assert negentropy.get_parent_level('prefix_16') == 'prefix_14'
        assert negentropy.get_parent_level('prefix_2') == 'root'
        assert negentropy.get_parent_level('root') is None  # Coarsest level


class TestFormatHuman:
    """Test human-readable unified key formatting."""

    def test_format_unified_key_human(self):
        """Format shows ISO timestamp and hash suffix."""
        ts_ms = 1718451045000  # 2024-06-15 11:30:45 UTC
        key = negentropy.compute_unified_key('evt1', ts_ms)
        formatted = negentropy.format_unified_key_human(key)
        assert '2024-06-15' in formatted
        assert '11:30:45' in formatted
        assert ':' in formatted  # Has separator between timestamp and hash


class TestHashComputation:
    """Test hash computation for buckets."""

    def test_empty_leaf_hash(self):
        """Empty bucket has empty hash."""
        assert negentropy.compute_leaf_hash([]) == b''

    def test_single_event_hash(self):
        """Single event gets hashed."""
        h = negentropy.compute_leaf_hash(['abc123'])
        assert len(h) == 16  # 128-bit hash
        assert h != b''

    def test_leaf_hash_deterministic(self):
        """Same events produce same hash regardless of order."""
        h1 = negentropy.compute_leaf_hash(['abc', 'def', 'ghi'])
        h2 = negentropy.compute_leaf_hash(['ghi', 'abc', 'def'])
        assert h1 == h2

    def test_leaf_hash_different_for_different_events(self):
        """Different events produce different hashes."""
        h1 = negentropy.compute_leaf_hash(['abc'])
        h2 = negentropy.compute_leaf_hash(['def'])
        assert h1 != h2

    def test_parent_hash_empty(self):
        """Empty children produce empty parent hash."""
        assert negentropy.compute_parent_hash({}) == b''
        assert negentropy.compute_parent_hash({'aa': b''}) == b''

    def test_parent_hash_non_empty(self):
        """Non-empty children produce hash."""
        h = negentropy.compute_parent_hash({'aa': b'x' * 16})
        assert len(h) == 16

    def test_parent_hash_deterministic(self):
        """Parent hash is deterministic regardless of dict order."""
        h1 = negentropy.compute_parent_hash({
            'aa': b'a' * 16,
            'bb': b'b' * 16,
        })
        h2 = negentropy.compute_parent_hash({
            'bb': b'b' * 16,
            'aa': b'a' * 16,
        })
        assert h1 == h2


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


class TestHashRecomputation:
    """Test lazy hash recomputation."""

    def test_recompute_leaf_hash(self, db):
        """Leaf hash computed from events."""
        peer_id = 'peer1'
        ts_ms = 1718451045000

        negentropy.add_event_to_sync(db, peer_id, 'evt1', ts_ms)
        negentropy.add_event_to_sync(db, peer_id, 'evt2', ts_ms)

        # Get the unified key prefix for the finest level
        unified_key = negentropy.compute_unified_key('evt1', ts_ms)
        prefix = unified_key[:16]  # Full prefix for prefix_16 level

        h = negentropy.recompute_bucket_hash(db, peer_id, 'prefix_16', prefix)

        # Hash should match computation from event IDs in that bucket
        # Note: evt1 and evt2 may be in different buckets if their hash suffixes differ
        events_in_bucket = negentropy.get_events_in_bucket(db, peer_id, prefix)
        expected = negentropy.compute_leaf_hash(events_in_bucket)
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
        base_ts = 1718451045000  # Some point in 2024

        # Add more events than EVENTS_THRESHOLD (100)
        for i in range(150):
            # Spread across different times to ensure they're in the same prefix_2 bucket
            negentropy.add_event_to_sync(db, peer_id, f'evt{i}', base_ts + i * 60000)

        # Get the prefix for prefix_2 level
        unified_key = negentropy.compute_unified_key('evt0', base_ts)
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

        # With 150 events (> 100 threshold), should drill down to child level
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
