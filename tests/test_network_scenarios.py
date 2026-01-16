"""CI network scenario tests.

Tests protocol behavior under realistic network conditions:
- High latency environments
- Lossy networks
- Network partitions and healing
- Jittery connections
- Burst packet loss
"""
import sqlite3
import random
import pytest
from core.db import Database
from core import schema
from core.db import create_unsafe_db
from core import queues
from core import network_config


@pytest.fixture
def db():
    """Create in-memory database for testing."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)
    return db


@pytest.fixture(autouse=True)
def reset_network_config():
    """Reset network config before each test."""
    network_config.reset_network_config()
    random.seed(42)
    yield
    network_config.reset_network_config()


class TestHighLatencyScenarios:
    """Tests for high-latency network environments."""

    def test_satellite_latency(self, db):
        """Simulate satellite internet (~600ms RTT)."""
        network_config.set_network_config(
            network_config.NetworkConfig(latency_ms=300, jitter_ms=50)
        )
        unsafedb = create_unsafe_db(db)

        # Send burst of packets
        for i in range(10):
            queues.incoming.add(f"msg_{i}".encode(), t_ms=0, unsafedb=unsafedb)
        db.commit()

        # Nothing should be delivered before 200ms (well before latency - jitter)
        received_early = queues.incoming.drain(100, current_time_ms=200, unsafedb=unsafedb)
        assert len(received_early) == 0

        # All should be delivered by 500ms (300 + 4 stddev of jitter = 300 + 200)
        received_all = queues.incoming.drain(100, current_time_ms=500, unsafedb=unsafedb)
        assert len(received_all) == 10

    def test_intercontinental_latency(self, db):
        """Simulate intercontinental link (~150ms one-way)."""
        network_config.set_network_config(
            network_config.NetworkConfig(latency_ms=150, jitter_ms=20)
        )
        unsafedb = create_unsafe_db(db)

        # Simulate request/response pattern
        queues.incoming.add(b"request", t_ms=0, unsafedb=unsafedb)
        db.commit()

        # Response arrives after latency
        received = queues.incoming.drain(10, current_time_ms=100, unsafedb=unsafedb)
        assert len(received) == 0

        received = queues.incoming.drain(10, current_time_ms=200, unsafedb=unsafedb)
        assert len(received) == 1


class TestLossyNetworkScenarios:
    """Tests for networks with packet loss."""

    def test_mobile_network_loss(self, db):
        """Simulate mobile network with 5% loss."""
        network_config.set_network_config(
            network_config.NetworkConfig(
                latency_ms=50,
                jitter_ms=30,
                packet_loss_rate=0.05
            )
        )
        unsafedb = create_unsafe_db(db)

        # Send 100 packets
        for i in range(100):
            queues.incoming.add(f"packet_{i}".encode(), t_ms=0, unsafedb=unsafedb)
        db.commit()

        received = queues.incoming.drain(100, current_time_ms=200, unsafedb=unsafedb)
        # With 5% loss, expect 90-100 packets
        assert 85 <= len(received) <= 100

    def test_congested_wifi(self, db):
        """Simulate congested WiFi with 15% loss and high jitter."""
        network_config.set_network_config(
            network_config.NetworkConfig(
                latency_ms=20,
                jitter_ms=50,  # High jitter
                packet_loss_rate=0.15
            )
        )
        unsafedb = create_unsafe_db(db)

        for i in range(100):
            queues.incoming.add(f"packet_{i}".encode(), t_ms=0, unsafedb=unsafedb)
        db.commit()

        received = queues.incoming.drain(100, current_time_ms=200, unsafedb=unsafedb)
        # With 15% loss, expect 75-95 packets
        assert 70 <= len(received) <= 95

    def test_burst_loss_recovery(self, db):
        """Test protocol handles burst packet loss."""
        network_config.set_network_config(
            network_config.NetworkConfig(
                latency_ms=10,
                burst_loss_probability=0.2,
                burst_loss_length=3
            )
        )
        unsafedb = create_unsafe_db(db)

        # Send 50 packets
        delivered = 0
        for i in range(50):
            if queues.incoming.add(f"packet_{i}".encode(), t_ms=0, unsafedb=unsafedb):
                delivered += 1
        db.commit()

        # Some packets should survive burst loss
        # With 20% burst prob and length 3, expect ~40% loss = ~30 delivered
        received = queues.incoming.drain(100, current_time_ms=100, unsafedb=unsafedb)
        assert 15 <= len(received) <= 45


class TestNetworkPartitionScenarios:
    """Tests for network partition and healing scenarios."""

    def test_partition_and_heal(self, db):
        """Test network partition followed by healing."""
        unsafedb = create_unsafe_db(db)

        # Phase 1: Normal operation (drain at t+1 for 1ms latency)
        queues.incoming.add(b"msg_1", t_ms=0, unsafedb=unsafedb, from_peer="alice")
        db.commit()
        received = queues.incoming.drain(10, current_time_ms=1, unsafedb=unsafedb)
        assert len(received) == 1

        # Phase 2: Partition alice
        network_config.partition_peer("alice")

        result = queues.incoming.add(b"msg_2", t_ms=100, unsafedb=unsafedb, from_peer="alice")
        assert result is False

        # Bob can still communicate
        result = queues.incoming.add(b"msg_3", t_ms=100, unsafedb=unsafedb, from_peer="bob")
        assert result is True

        # Phase 3: Heal partition
        network_config.unpartition_peer("alice")

        result = queues.incoming.add(b"msg_4", t_ms=200, unsafedb=unsafedb, from_peer="alice")
        assert result is True

        db.commit()
        received = queues.incoming.drain(10, current_time_ms=201, unsafedb=unsafedb)
        assert len(received) == 2  # msg_3 and msg_4

    def test_asymmetric_partition(self, db):
        """Test where only one direction is partitioned."""
        unsafedb = create_unsafe_db(db)

        # Partition alice (can't send)
        network_config.partition_peer("alice")

        # Alice can't send to bob
        result = queues.incoming.add(b"alice_to_bob", t_ms=0, unsafedb=unsafedb, from_peer="alice", to_peer="bob")
        assert result is False

        # But bob CAN send (to non-partitioned charlie)
        result = queues.incoming.add(b"bob_to_charlie", t_ms=0, unsafedb=unsafedb, from_peer="bob", to_peer="charlie")
        assert result is True

    def test_multiple_partitions(self, db):
        """Test multiple peers partitioned simultaneously."""
        unsafedb = create_unsafe_db(db)

        # Partition alice and bob
        network_config.partition_peer("alice")
        network_config.partition_peer("bob")

        # Neither can send
        assert queues.incoming.add(b"a", t_ms=0, unsafedb=unsafedb, from_peer="alice") is False
        assert queues.incoming.add(b"b", t_ms=0, unsafedb=unsafedb, from_peer="bob") is False

        # Charlie can still send
        assert queues.incoming.add(b"c", t_ms=0, unsafedb=unsafedb, from_peer="charlie") is True


class TestRealisticScenarios:
    """Combined realistic network scenarios."""

    def test_mobile_to_datacenter(self, db):
        """Mobile client connecting to datacenter."""
        network_config.set_network_config(
            network_config.NetworkConfig(
                latency_ms=80,
                jitter_ms=40,
                packet_loss_rate=0.02
            )
        )
        unsafedb = create_unsafe_db(db)

        # Simulate 1 second of message exchange (10 msgs/sec)
        for i in range(10):
            queues.incoming.add(f"msg_{i}".encode(), t_ms=i * 100, unsafedb=unsafedb)
        db.commit()

        # Drain at end of simulation
        received = queues.incoming.drain(100, current_time_ms=1200, unsafedb=unsafedb)
        # Most should arrive (98% expected)
        assert len(received) >= 8

    def test_flaky_connection_burst(self, db):
        """Flaky connection with burst loss during sync."""
        network_config.set_network_config(
            network_config.NetworkConfig(
                latency_ms=30,
                jitter_ms=20,
                burst_loss_probability=0.1,
                burst_loss_length=5
            )
        )
        unsafedb = create_unsafe_db(db)

        # Simulate bulk sync (100 events)
        delivered = 0
        for i in range(100):
            random.seed(i)  # Vary randomness
            if queues.incoming.add(f"event_{i}".encode(), t_ms=0, unsafedb=unsafedb):
                delivered += 1
        db.commit()

        received = queues.incoming.drain(100, current_time_ms=200, unsafedb=unsafedb)
        # With 10% burst prob and length 5, expect significant loss but majority delivered
        assert 40 <= len(received) <= 90

    def test_minimal_latency_local(self, db):
        """Local network with minimal latency (loopback/LAN).

        Note: We use latency_ms=1 instead of 0 because SimPy has an edge case
        where events scheduled at the current time may not be processed.
        """
        network_config.set_network_config(
            network_config.NetworkConfig(latency_ms=1, jitter_ms=0, packet_loss_rate=0.0)
        )
        unsafedb = create_unsafe_db(db)

        # All packets should be available after 1ms latency
        for i in range(100):
            queues.incoming.add(f"msg_{i}".encode(), t_ms=0, unsafedb=unsafedb)
        db.commit()

        received = queues.incoming.drain(100, current_time_ms=1, unsafedb=unsafedb)
        assert len(received) == 100


class TestNetworkConfigPresets:
    """Test common network condition presets."""

    @pytest.mark.parametrize("preset,expected_delivery_rate", [
        # (latency, jitter, loss) -> min expected delivery rate
        ((10, 5, 0.0), 1.0),      # LAN
        ((50, 20, 0.01), 0.95),   # Good broadband
        ((100, 30, 0.03), 0.90),  # Average broadband
        ((200, 50, 0.05), 0.85),  # Mobile 4G
        ((500, 100, 0.10), 0.70), # Mobile 3G/poor
    ])
    def test_network_presets(self, db, preset, expected_delivery_rate):
        """Test various network condition presets."""
        latency, jitter, loss = preset
        network_config.set_network_config(
            network_config.NetworkConfig(
                latency_ms=latency,
                jitter_ms=jitter,
                packet_loss_rate=loss
            )
        )
        unsafedb = create_unsafe_db(db)

        num_packets = 100
        for i in range(num_packets):
            queues.incoming.add(f"pkt_{i}".encode(), t_ms=0, unsafedb=unsafedb)
        db.commit()

        # Wait long enough for all packets to potentially arrive
        max_delay = latency + 3 * jitter  # 3 sigma
        received = queues.incoming.drain(num_packets, current_time_ms=max_delay + 100, unsafedb=unsafedb)

        actual_rate = len(received) / num_packets
        # Allow 15% variance below expected due to randomness
        assert actual_rate >= expected_delivery_rate - 0.15, \
            f"Expected ~{expected_delivery_rate*100}% delivery, got {actual_rate*100}%"
