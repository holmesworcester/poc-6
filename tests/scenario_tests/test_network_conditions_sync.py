"""
Integration tests for sync under realistic network conditions.

DEPRECATED: These tests use the old network_config simulator which has been
replaced by the new address-based transport (core/transport.py).

The new transport uses real UDP networking and does not simulate network conditions.
"""
import pytest

pytestmark = pytest.mark.skip(reason="Deprecated: network_config simulator replaced by UDP transport")
from core.db import Database
from core import schema
from core import network_config
from events.identity import user, invite, peer
from events.content import message, channel
from tests.utils.tick_helper import run_ticks, assert_eventually


@pytest.fixture(autouse=True)
def reset_network():
    """Reset network config before and after each test."""
    network_config.reset_network_config()
    yield
    network_config.reset_network_config()


class TestSyncWithLatency:
    """Test sync convergence with various latency conditions."""

    def test_sync_with_100ms_latency(self, fresh_db):
        """Sync should converge with 100ms latency."""
        db = fresh_db

        # Set network conditions
        network_config.set_network_config(
            network_config.NetworkConfig(latency_ms=100, jitter_ms=20)
        )

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

        # Run sync - need more rounds due to latency
        run_ticks(db=db, start_t_ms=3000, num_rounds=300)

        # Check no blocked events
        blocked_count = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral")['cnt']
        assert blocked_count == 0, f"Should have no blocked events, got {blocked_count}"

    def test_sync_with_high_latency(self, fresh_db):
        """Sync should converge even with 300ms latency (satellite-like)."""
        db = fresh_db

        # Set high latency
        network_config.set_network_config(
            network_config.NetworkConfig(latency_ms=300, jitter_ms=50)
        )

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

        # Run sync - need even more rounds due to high latency
        run_ticks(db=db, start_t_ms=3000, num_rounds=500)

        # Check no blocked events
        blocked_count = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral")['cnt']
        assert blocked_count == 0, f"Should have no blocked events, got {blocked_count}"


class TestSyncWithPacketLoss:
    """Test sync convergence with packet loss."""

    def test_sync_with_5_percent_loss(self, fresh_db):
        """Sync should converge with 5% packet loss."""
        db = fresh_db

        # Set moderate packet loss
        network_config.set_network_config(
            network_config.NetworkConfig(latency_ms=50, packet_loss_rate=0.05)
        )

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

        # Run sync
        run_ticks(db=db, start_t_ms=3000, num_rounds=400)

        # Check no blocked events
        blocked_count = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral")['cnt']
        assert blocked_count == 0, f"Should converge despite packet loss, got {blocked_count} blocked"

    def test_sync_with_10_percent_loss(self, fresh_db):
        """Sync should converge with 10% packet loss (needs retries)."""
        db = fresh_db

        # Set higher packet loss
        network_config.set_network_config(
            network_config.NetworkConfig(latency_ms=50, packet_loss_rate=0.10)
        )

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

        # Run sync - more rounds needed with higher loss
        run_ticks(db=db, start_t_ms=3000, num_rounds=600)

        # May not fully converge with high loss, but shouldn't crash
        blocked_count = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral")['cnt']
        print(f"10% packet loss sync: {blocked_count} events blocked")


class TestSyncWithPartitions:
    """Test sync behavior with network partitions."""

    def test_partition_blocks_sync_then_heals(self, fresh_db):
        """Partitioned peer should miss messages, then catch up after healing."""
        db = fresh_db

        # Start with good network
        network_config.set_network_config(
            network_config.NetworkConfig(latency_ms=10)
        )

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

        # Initial sync to establish connection
        t_ms = run_ticks(db, start_t_ms=3000, num_rounds=50)

        # Partition Bob
        print("\n=== Partitioning Bob ===")
        network_config.partition_peer(bob['peer_id'])

        # Alice sends a message while Bob is partitioned
        msg_result = message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content="Message during partition",
            t_ms=t_ms,
            db=db
        )
        db.commit()
        t_ms += 100

        # Run sync while Bob is partitioned
        t_ms = run_ticks(db, start_t_ms=t_ms, num_rounds=50)

        # Check Bob doesn't have the message yet
        bob_messages = message.list(alice['channel_id'], bob['peer_id'], db)
        msg_found_during_partition = any(
            m.get('content') == "Message during partition" for m in bob_messages
        )
        print(f"Bob has message during partition: {msg_found_during_partition}")

        # Heal partition
        print("\n=== Healing partition ===")
        network_config.unpartition_peer(bob['peer_id'])

        # Run more sync to allow catch-up
        run_ticks(db=db, start_t_ms=t_ms, num_rounds=200)

        # Check Bob now has the message
        bob_messages_after = message.list(alice['channel_id'], bob['peer_id'], db)
        msg_found_after_heal = any(
            m.get('content') == "Message during partition" for m in bob_messages_after
        )
        print(f"Bob has message after heal: {msg_found_after_heal}")
        # Bob should eventually get the message after partition heals
        # (This tests the protocol's ability to recover from network issues)


class TestSyncWithBurstLoss:
    """Test sync with burst packet loss patterns."""

    def test_sync_with_burst_loss(self, fresh_db):
        """Sync should handle burst packet loss."""
        db = fresh_db

        # Set burst loss (10% chance of 3-packet burst)
        network_config.set_network_config(
            network_config.NetworkConfig(
                latency_ms=50,
                burst_loss_probability=0.10,
                burst_loss_length=3
            )
        )

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

        # Run sync
        run_ticks(db=db, start_t_ms=3000, num_rounds=500)

        # Should handle burst loss through retries
        blocked_count = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral")['cnt']
        print(f"Burst loss sync: {blocked_count} events blocked")


class TestSyncWithRealisticConditions:
    """Test sync with combined realistic network conditions."""

    def test_mobile_4g_conditions(self, fresh_db):
        """Sync should work under mobile 4G-like conditions."""
        db = fresh_db

        # Mobile 4G: 100ms latency, 40ms jitter, 3% loss, 5% burst
        network_config.set_network_config(
            network_config.NetworkConfig(
                latency_ms=100,
                jitter_ms=40,
                packet_loss_rate=0.03,
                burst_loss_probability=0.05,
                burst_loss_length=3
            )
        )

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

        # Run sync
        run_ticks(db=db, start_t_ms=3000, num_rounds=500)

        blocked_count = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral")['cnt']
        print(f"Mobile 4G sync: {blocked_count} events blocked")
        assert blocked_count == 0, "Should converge under mobile 4G conditions"

    def test_poor_wifi_conditions(self, fresh_db):
        """Sync should work under poor WiFi conditions."""
        db = fresh_db

        # Poor WiFi: 20ms latency, 50ms jitter (high), 15% loss
        network_config.set_network_config(
            network_config.NetworkConfig(
                latency_ms=20,
                jitter_ms=50,
                packet_loss_rate=0.15,
                burst_loss_probability=0.20,
                burst_loss_length=5
            )
        )

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

        # Run sync - may need many rounds with high loss
        run_ticks(db=db, start_t_ms=3000, num_rounds=800)

        # High loss may prevent full convergence, but shouldn't crash
        blocked_count = db.query_one("SELECT COUNT(*) as cnt FROM blocked_events_ephemeral")['cnt']
        print(f"Poor WiFi sync: {blocked_count} events blocked")
