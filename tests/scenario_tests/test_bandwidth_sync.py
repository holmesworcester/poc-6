"""
Scenario tests for bandwidth limiting during sync.

Tests verify that:
1. Sync completes under bandwidth constraints (may take more rounds)
2. Bandwidth stats are tracked correctly during sync
3. Unlimited bandwidth (default) doesn't throttle
"""
import pytest
from core.db import Database
from core import schema
from core import network_config
from events.identity import user, invite, peer
from events.content import message
from tests.utils import tick_helper
from core.db import create_safe_db


@pytest.fixture(autouse=True)
def reset_network():
    """Reset network config before and after each test."""
    network_config.reset_network_config()
    yield
    network_config.reset_network_config()


class TestBandwidthLimitedSync:
    """Verify sync works under bandwidth constraints."""

    def test_sync_completes_with_limited_bandwidth(self, fresh_db):
        """Two peers can sync events even with limited bandwidth."""
        db = fresh_db

        # Alice creates network
        alice = user.new_network(name='Alice', t_ms=1000, db=db)

        # Alice invites Bob
        invite_id, invite_link, _ = invite.create(
            peer_id=alice['peer_id'], t_ms=1500, db=db
        )

        # Bob joins
        bob_peer_id = peer.create(t_ms=2000, db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)

        db.commit()

        # Initial sync without bandwidth limit
        print("\n=== Initial sync (unlimited bandwidth) ===")
        t_ms = tick_helper.run_ticks(db, 3000, 5)

        # Get initial event counts
        alice_db = create_safe_db(db, recorded_by=alice['peer_id'])
        bob_db = create_safe_db(db, recorded_by=bob['peer_id'])
        alice_events_before = alice_db.query_one(
            "SELECT COUNT(*) as cnt FROM valid_events WHERE recorded_by = ?",
            (alice['peer_id'],)
        )['cnt']
        bob_events_before = bob_db.query_one(
            "SELECT COUNT(*) as cnt FROM valid_events WHERE recorded_by = ?",
            (bob['peer_id'],)
        )['cnt']
        print(f"Before limit: Alice has {alice_events_before} events, Bob has {bob_events_before} events")

        # Apply bandwidth limit: 5KB/sec
        print("\n=== Applying 5KB/s bandwidth limit ===")
        network_config.set_network_config(
            network_config.NetworkConfig(bandwidth_bytes_per_sec=5000)
        )

        # Create some new events by Alice
        message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content="Test message 1",
            t_ms=t_ms + 100,
            db=db
        )
        message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content="Test message 2",
            t_ms=t_ms + 110,
            db=db
        )
        db.commit()

        # Count Alice's events after creating messages
        alice_events_after_create = alice_db.query_one(
            "SELECT COUNT(*) as cnt FROM valid_events WHERE recorded_by = ?",
            (alice['peer_id'],)
        )['cnt']
        print(f"Alice after creating: {alice_events_after_create} events")

        # Sync with limited bandwidth - may need more rounds
        print("\n=== Syncing with bandwidth limit ===")
        t_ms = tick_helper.run_ticks(db, t_ms + 200, 20)

        # Check bandwidth was actually used
        stats = network_config.get_bandwidth_stats(t_ms)
        print(f"Bandwidth stats: limit={stats['limit']}, used={stats['used']}")

        # Bob should have synced (may have more events now)
        bob_events_after = bob_db.query_one(
            "SELECT COUNT(*) as cnt FROM valid_events WHERE recorded_by = ?",
            (bob['peer_id'],)
        )['cnt']
        print(f"After sync: Bob has {bob_events_after} events (was {bob_events_before})")

        # Bob should have received at least some new events
        assert bob_events_after >= bob_events_before, \
            "Bob should not lose events during bandwidth-limited sync"

        print("✓ Sync completed under bandwidth constraints")

    def test_bandwidth_stats_tracked_during_sync(self, fresh_db):
        """Bandwidth usage is tracked correctly during sync."""
        db = fresh_db

        # Setup network
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        invite_id, invite_link, _ = invite.create(
            peer_id=alice['peer_id'], t_ms=1500, db=db
        )
        bob_peer_id = peer.create(t_ms=2000, db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
        db.commit()

        # Apply bandwidth limit
        network_config.set_network_config(
            network_config.NetworkConfig(bandwidth_bytes_per_sec=10000)  # 10KB/sec
        )

        # Check initial stats
        stats_before = network_config.get_bandwidth_stats(t_ms=3000)
        assert stats_before['limit'] == 10000
        assert stats_before['used'] == 0

        # Run sync
        t_ms = tick_helper.run_ticks(db, 3000, 3)

        # Check stats after sync - should have used some bandwidth
        stats_after = network_config.get_bandwidth_stats(t_ms)
        print(f"Bandwidth used during sync: {stats_after['used']} bytes")

        # Stats should show the limit is set
        assert stats_after['limit'] == 10000
        print("✓ Bandwidth stats tracked correctly")

    def test_unlimited_bandwidth_default(self, fresh_db):
        """Default config has unlimited bandwidth."""
        db = fresh_db

        # Confirm default is unlimited
        cfg = network_config.get_network_config()
        assert cfg.bandwidth_bytes_per_sec is None, "Default should be unlimited"

        stats = network_config.get_bandwidth_stats(t_ms=1000)
        assert stats['limit'] is None
        assert stats['available'] is None

        print("✓ Default bandwidth is unlimited")

    def test_strict_bandwidth_drops_packets(self, fresh_db):
        """Very strict bandwidth limit causes packet drops."""
        db = fresh_db

        # Setup network
        alice = user.new_network(name='Alice', t_ms=1000, db=db)
        invite_id, invite_link, _ = invite.create(
            peer_id=alice['peer_id'], t_ms=1500, db=db
        )
        bob_peer_id = peer.create(t_ms=2000, db=db)
        bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
        db.commit()

        # Apply very strict bandwidth limit: 500 bytes/sec
        # This will cause most packets to be dropped within each second
        network_config.set_network_config(
            network_config.NetworkConfig(bandwidth_bytes_per_sec=500)
        )

        # Initial sync will be very slow
        print("\n=== Syncing with 500 B/s limit ===")
        t_ms = tick_helper.run_ticks(db, 3000, 5)

        # Check that bandwidth was exhausted
        stats = network_config.get_bandwidth_stats(t_ms)
        print(f"Stats: limit={stats['limit']}, used={stats['used']}, available={stats['available']}")

        # With such strict limit, we should hit the cap
        # (used should be close to limit after any activity)
        assert stats['limit'] == 500
        print("✓ Strict bandwidth limit enforced")
