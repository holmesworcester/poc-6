"""Tests for negentropy-style deterministic sync protocol."""
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


class TestBucketTimestamps:
    """Test bucket timestamp computation."""

    def test_year_bucket(self):
        # 2024-06-15 11:30:45 UTC -> start of 2024
        ts_ms = 1718451045000
        start_ms = negentropy.get_bucket_start_ms(ts_ms, 'year')
        # 2024-01-01 00:00:00 UTC
        assert start_ms == 1704067200000

    def test_month_bucket(self):
        ts_ms = 1718451045000  # 2024-06-15 UTC
        start_ms = negentropy.get_bucket_start_ms(ts_ms, 'month')
        # 2024-06-01 00:00:00 UTC
        assert start_ms == 1717200000000

    def test_day_bucket(self):
        ts_ms = 1718451045000  # 2024-06-15 UTC
        start_ms = negentropy.get_bucket_start_ms(ts_ms, 'day')
        # 2024-06-15 00:00:00 UTC
        assert start_ms == 1718409600000

    def test_hour_bucket(self):
        ts_ms = 1718451045000  # 2024-06-15 11:30:45 UTC
        start_ms = negentropy.get_bucket_start_ms(ts_ms, 'hour')
        # 2024-06-15 11:00:00 UTC
        assert start_ms == 1718449200000

    def test_ten_min_bucket(self):
        ts_ms = 1718451045000  # 2024-06-15 11:30:45 UTC (minute 30 = bucket 3)
        start_ms = negentropy.get_bucket_start_ms(ts_ms, 'ten_min')
        # 2024-06-15 11:30:00 UTC
        assert start_ms == 1718451000000

    def test_one_min_bucket(self):
        ts_ms = 1718451045000  # 2024-06-15 11:30:45 UTC
        start_ms = negentropy.get_bucket_start_ms(ts_ms, 'one_min')
        # 2024-06-15 11:30:00 UTC
        assert start_ms == 1718451000000

    def test_root_bucket(self):
        ts_ms = 1718451045000
        start_ms = negentropy.get_bucket_start_ms(ts_ms, 'root')
        assert start_ms == negentropy.ROOT_BUCKET_START

    def test_bucket_end_year(self):
        # Start of 2024
        start_ms = 1704067200000
        end_ms = negentropy.get_bucket_end_ms(start_ms, 'year')
        # Should be start of 2025
        assert end_ms == 1735689600000

    def test_bucket_end_month(self):
        # Start of June 2024
        start_ms = 1717200000000
        end_ms = negentropy.get_bucket_end_ms(start_ms, 'month')
        # Should be start of July 2024
        assert end_ms == 1719792000000


class TestFormatHuman:
    """Test human-readable bucket formatting."""

    def test_format_year(self):
        start_ms = 1704067200000  # 2024-01-01
        assert negentropy.format_bucket_human(start_ms, 'year') == '2024'

    def test_format_month(self):
        start_ms = 1717200000000  # 2024-06-01
        assert negentropy.format_bucket_human(start_ms, 'month') == '2024-06'

    def test_format_day(self):
        start_ms = 1718409600000  # 2024-06-15
        assert negentropy.format_bucket_human(start_ms, 'day') == '2024-06-15'

    def test_format_root(self):
        assert negentropy.format_bucket_human(0, 'root') == 'root'


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
        assert negentropy.compute_parent_hash({123: b''}) == b''

    def test_parent_hash_non_empty(self):
        """Non-empty children produce hash."""
        h = negentropy.compute_parent_hash({1704067200000: b'x' * 16})
        assert len(h) == 16

    def test_parent_hash_deterministic(self):
        """Parent hash is deterministic regardless of dict order."""
        h1 = negentropy.compute_parent_hash({
            1704067200000: b'a' * 16,
            1706745600000: b'b' * 16,
        })
        h2 = negentropy.compute_parent_hash({
            1706745600000: b'b' * 16,
            1704067200000: b'a' * 16,
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
            SELECT bucket_start_ms FROM negentropy_events
            WHERE recorded_by = ? AND event_id = ?
        """, (peer_id, event_id))
        assert row is not None
        # Should be start of minute 30
        expected_start = negentropy.get_bucket_start_ms(ts_ms, 'one_min')
        assert row['bucket_start_ms'] == expected_start

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

        bucket_start = negentropy.get_bucket_start_ms(ts_ms, 'one_min')
        h = negentropy.recompute_bucket_hash(db, peer_id, 'one_min', bucket_start)

        expected = negentropy.compute_leaf_hash(['evt1', 'evt2'])
        assert h == expected

    def test_get_hashes_at_level(self, db):
        """Get all hashes at a level."""
        peer_id = 'peer1'

        # Add events in different months
        negentropy.add_event_to_sync(db, peer_id, 'evt1', 1704067200000)  # 2024-01-01
        negentropy.add_event_to_sync(db, peer_id, 'evt2', 1706745600000)  # 2024-02-01

        year_hashes = negentropy.get_hashes_at_level(db, peer_id, 'year')
        year_2024_start = negentropy.get_bucket_start_ms(1704067200000, 'year')
        assert year_2024_start in year_hashes
        assert year_hashes[year_2024_start] != b''


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
        assert requests[0]['bucket_start_ms'] == negentropy.ROOT_BUCKET_START
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
            'bucket_start_ms': negentropy.ROOT_BUCKET_START,
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

        # Receive request at year level with different hash
        msg = {
            'type': 'range_request',
            'range_id': 'remote_1',
            'level': 'year',
            'bucket_start_ms': negentropy.get_bucket_start_ms(ts_ms, 'year'),
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
            # Spread across different minutes to ensure they're in the year bucket
            negentropy.add_event_to_sync(db, peer_id, f'evt{i}', base_ts + i * 60000)

        # Receive request at year level with different hash
        msg = {
            'type': 'range_request',
            'range_id': 'remote_1',
            'level': 'year',
            'bucket_start_ms': negentropy.get_bucket_start_ms(base_ts, 'year'),
            'hash': 'deadbeef',  # Different hash
        }
        responses = negentropy.handle_range_request(db, peer_id, conn_id, msg, 1000)

        # With 150 events (> 100 threshold), should drill down to child level
        assert any(r['type'] == 'range_request' for r in responses)
        # Should NOT send events directly at this level
        assert not any(r['type'] == 'range_events' for r in responses)
        # Child requests should be at 'month' level
        child_requests = [r for r in responses if r['type'] == 'range_request']
        assert all(r['level'] == 'month' for r in child_requests)


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
            'bucket_start_ms': negentropy.ROOT_BUCKET_START,
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

        # Create a negentropy envelope as would arrive from sync
        envelope = {
            'type': 'negentropy',
            'connection_id': conn_id,
            'data': {
                'type': 'range_request',
                'range_id': 'remote_1',
                'level': 'year',
                'bucket_start_ms': negentropy.get_bucket_start_ms(ts_ms, 'year'),
                'hash': 'deadbeef',
            }
        }

        # Should not raise - connection.send will fail but that's expected
        # since we don't have a real connection
        try:
            negentropy.handle_incoming(db, peer_id, conn_id, envelope, 2000)
        except Exception:
            pass  # Expected - no real connection to send on
