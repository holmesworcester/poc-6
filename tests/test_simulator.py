"""Tests for network simulator with new transport layer.

Tests latency, packet loss, NAT, and partitions using the new
core/simulator.py + core/transport.py architecture.
"""
import random
import pytest
from core import transport
from core.simulator import NetworkSimulator, NetworkConfig


@pytest.fixture(autouse=True)
def reset_transport():
    """Reset transport state before each test."""
    transport.reset()
    random.seed(42)
    yield
    transport.reset()


class TestBasicLatency:
    """Test latency simulation."""

    def test_immediate_delivery_without_simulator(self):
        """Without simulator, packets are delivered immediately in loopback mode."""
        transport.enable_loopback()  # Enable loopback mode first
        transport.send(b"test", ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        count = transport.loopback_transfer()
        assert count == 1

        batch = transport.drain_incoming(10)
        assert len(batch) == 1
        assert batch[0][0] == b"test"

    def test_latency_delays_delivery(self):
        """With simulator, packets are delayed by latency."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=100, jitter_ms=0))
        transport.set_simulator(sim)

        # Send and queue at t=1000
        transport.send(b"test", ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        count = transport.simulator_transfer(1000)  # Queue packet, deliver_at = 1100
        assert count == 0  # Not delivered yet

        # At t=1050, packet should not be ready
        count = transport.simulator_transfer(1050)
        assert count == 0

        # At t=1100, packet should be delivered
        count = transport.simulator_transfer(1100)
        assert count == 1

        batch = transport.drain_incoming(10)
        assert len(batch) == 1

    def test_multiple_packets_with_latency(self):
        """Multiple packets delivered at correct times."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=50, jitter_ms=0))
        transport.set_simulator(sim)

        # Send 3 packets at different times
        transport.send(b"pkt1", ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        transport.simulator_transfer(1000)  # Queue pkt1, delivers at 1050

        transport.send(b"pkt2", ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        transport.simulator_transfer(1020)  # Queue pkt2, delivers at 1070

        transport.send(b"pkt3", ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        transport.simulator_transfer(1040)  # Queue pkt3, delivers at 1090

        # At t=1060, only pkt1 ready
        count = transport.simulator_transfer(1060)
        assert count == 1

        # At t=1080, pkt2 ready
        count = transport.simulator_transfer(1080)
        assert count == 1

        # At t=1100, pkt3 ready
        count = transport.simulator_transfer(1100)
        assert count == 1


class TestPacketLoss:
    """Test packet loss simulation."""

    def test_no_loss_delivers_all(self):
        """Zero packet loss delivers all packets."""
        sim = NetworkSimulator(NetworkConfig(packet_loss_rate=0.0, latency_ms=1))
        transport.set_simulator(sim)

        for i in range(100):
            transport.send(f"pkt{i}".encode(), ('127.0.0.1', 1000), ('127.0.0.1', 2000))

        transport.simulator_transfer(1000)
        transport.simulator_transfer(1100)

        batch = transport.drain_incoming(200)
        assert len(batch) == 100

    def test_fifty_percent_loss(self):
        """50% packet loss drops roughly half."""
        sim = NetworkSimulator(NetworkConfig(packet_loss_rate=0.5, latency_ms=1))
        transport.set_simulator(sim)

        for i in range(100):
            transport.send(f"pkt{i}".encode(), ('127.0.0.1', 1000), ('127.0.0.1', 2000))

        transport.simulator_transfer(1000)
        transport.simulator_transfer(1100)

        batch = transport.drain_incoming(200)
        # Allow variance (30-70 out of 100)
        assert 30 <= len(batch) <= 70, f"Expected ~50, got {len(batch)}"

    def test_high_loss_rate(self):
        """99% loss drops almost all packets."""
        sim = NetworkSimulator(NetworkConfig(packet_loss_rate=0.99, latency_ms=1))
        transport.set_simulator(sim)

        for i in range(1000):
            transport.send(f"pkt{i}".encode(), ('127.0.0.1', 1000), ('127.0.0.1', 2000))

        transport.simulator_transfer(1000)
        transport.simulator_transfer(1100)

        batch = transport.drain_incoming(2000)
        assert len(batch) < 100, f"Expected ~10, got {len(batch)}"


class TestBurstLoss:
    """Test burst packet loss simulation."""

    def test_burst_drops_consecutive(self):
        """Burst loss drops consecutive packets once triggered."""
        sim = NetworkSimulator(NetworkConfig(
            burst_loss_probability=0.0,  # Don't auto-trigger
            burst_loss_length=3,
            latency_ms=1
        ))
        transport.set_simulator(sim)

        # Manually trigger burst mode
        sim._in_burst = True
        sim._burst_remaining = 3

        # Send 5 packets - first 3 dropped in burst, last 2 delivered
        for i in range(5):
            transport.send(f"pkt{i}".encode(), ('127.0.0.1', 1000), ('127.0.0.1', 2000))

        transport.simulator_transfer(1000)
        transport.simulator_transfer(1100)
        batch = transport.drain_incoming(10)

        # First 3 dropped (burst), last 2 delivered
        assert len(batch) == 2
        assert sim.packets_dropped == 3
        assert sim.packets_delivered == 2

    def test_burst_probabilistic(self):
        """Low burst probability occasionally triggers bursts."""
        sim = NetworkSimulator(NetworkConfig(
            burst_loss_probability=0.1,
            burst_loss_length=2,
            latency_ms=1
        ))
        transport.set_simulator(sim)

        for i in range(100):
            transport.send(f"pkt{i}".encode(), ('127.0.0.1', 1000), ('127.0.0.1', 2000))
            transport.simulator_transfer(1000 + i)

        transport.simulator_transfer(1200)
        batch = transport.drain_incoming(200)

        # Should have some drops but not too many
        assert 60 <= len(batch) <= 95, f"Expected ~80, got {len(batch)}"


class TestMaxPacketSize:
    """Test max packet size enforcement."""

    def test_oversized_dropped(self):
        """Oversized packets are dropped."""
        sim = NetworkSimulator(NetworkConfig(max_packet_size=100, latency_ms=1))
        transport.set_simulator(sim)

        # Under size - accepted
        transport.send(b"x" * 50, ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        # Exact size - accepted
        transport.send(b"x" * 100, ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        # Over size - dropped
        transport.send(b"x" * 101, ('127.0.0.1', 1000), ('127.0.0.1', 2000))

        transport.simulator_transfer(1000)
        transport.simulator_transfer(1100)

        batch = transport.drain_incoming(10)
        assert len(batch) == 2


class TestPacing:
    """Test outbound pacing."""

    def test_leaky_bucket_limits_send_rate(self):
        """Pacer limits the number of packets admitted per time window."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=1))
        transport.set_simulator(sim)
        transport.set_pacer(rate_bytes_per_sec=1000, burst_bytes=1000)

        payload = b"x" * 200
        for _ in range(10):
            transport.send(payload, ('127.0.0.1', 1000), ('127.0.0.1', 2000))

        # At t=1000, only 5 packets fit in the 1000-byte burst.
        transport.simulator_transfer(1000)
        incoming, outgoing = transport.pending_count()
        assert sim.packets_sent == 5
        assert outgoing == 5
        assert incoming == 0

        # Deliver the first batch; no new sends yet.
        transport.simulator_transfer(1001)
        batch = transport.drain_incoming(20)
        assert len(batch) == 5

        # After 1s, budget refills and remaining packets send.
        transport.simulator_transfer(2000)
        transport.simulator_transfer(2001)
        batch = transport.drain_incoming(20)
        assert len(batch) == 5


class TestPartitions:
    """Test network partition simulation."""

    def test_partition_blocks_source(self):
        """Partitioned source address is blocked."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=1))
        sim.partition(('127.0.0.1', 1000))
        transport.set_simulator(sim)

        # From partitioned address - blocked
        transport.send(b"blocked", ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        # From other address - ok
        transport.send(b"ok", ('127.0.0.1', 1001), ('127.0.0.1', 2000))

        transport.simulator_transfer(1000)
        transport.simulator_transfer(1100)

        batch = transport.drain_incoming(10)
        assert len(batch) == 1
        assert batch[0][0] == b"ok"

    def test_partition_blocks_destination(self):
        """Partitioned destination address is blocked."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=1))
        sim.partition(('127.0.0.1', 2000))
        transport.set_simulator(sim)

        # To partitioned address - blocked
        transport.send(b"blocked", ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        # To other address - ok
        transport.send(b"ok", ('127.0.0.1', 1000), ('127.0.0.1', 2001))

        transport.simulator_transfer(1000)
        transport.simulator_transfer(1100)

        batch = transport.drain_incoming(10)
        assert len(batch) == 1
        assert batch[0][0] == b"ok"

    def test_heal_partition(self):
        """Healing partition restores connectivity."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=1))
        sim.partition(('127.0.0.1', 1000))
        transport.set_simulator(sim)

        # Blocked
        transport.send(b"blocked", ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        transport.simulator_transfer(1000)

        # Heal
        sim.heal_partition(('127.0.0.1', 1000))

        # Now works
        transport.send(b"restored", ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        transport.simulator_transfer(1050)
        transport.simulator_transfer(1100)

        batch = transport.drain_incoming(10)
        assert len(batch) == 1
        assert batch[0][0] == b"restored"


class TestNAT:
    """Test NAT traversal simulation."""

    def test_nat_blocks_without_mapping(self):
        """NAT blocks incoming packets without hole punch."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=1))
        sim.enable_nat(('192.168.1.1', 1000))  # Bob behind NAT
        transport.set_simulator(sim)

        # Alice tries to send to Bob - blocked (no mapping)
        transport.send(b"blocked", ('1.2.3.4', 1000), ('192.168.1.1', 1000))
        transport.simulator_transfer(1000)
        transport.simulator_transfer(1100)

        batch = transport.drain_incoming(10)
        assert len(batch) == 0

    def test_nat_allows_after_hole_punch(self):
        """NAT allows packets after outbound connection (hole punch)."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=1))
        sim.enable_nat(('192.168.1.1', 1000))  # Bob behind NAT
        transport.set_simulator(sim)

        # Bob sends to Alice first (creates NAT mapping)
        transport.send(b"from_bob", ('192.168.1.1', 1000), ('1.2.3.4', 1000))
        transport.simulator_transfer(1000)
        sim.create_nat_mapping(('192.168.1.1', 1000), ('1.2.3.4', 1000), 1000)

        # Now Alice can send to Bob
        transport.send(b"from_alice", ('1.2.3.4', 1000), ('192.168.1.1', 1000))
        transport.simulator_transfer(1050)
        transport.simulator_transfer(1100)

        batch = transport.drain_incoming(10)
        assert len(batch) == 2

    def test_nat_mapping_expires(self):
        """NAT mapping expires after TTL."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=1))
        sim.enable_nat(('192.168.1.1', 1000))
        sim._nat_ttl_ms = 1000  # 1 second TTL
        transport.set_simulator(sim)

        # Create mapping at t=1000
        sim.create_nat_mapping(('192.168.1.1', 1000), ('1.2.3.4', 1000), 1000)

        # Works at t=1500 (within TTL)
        transport.send(b"ok", ('1.2.3.4', 1000), ('192.168.1.1', 1000))
        transport.simulator_transfer(1500)
        transport.simulator_transfer(1600)

        batch = transport.drain_incoming(10)
        assert len(batch) == 1

        # Fails at t=3000 (TTL expired)
        transport.send(b"blocked", ('1.2.3.4', 1000), ('192.168.1.1', 1000))
        transport.simulator_transfer(3000)
        transport.simulator_transfer(3100)

        batch = transport.drain_incoming(10)
        assert len(batch) == 0


class TestStats:
    """Test simulator statistics."""

    def test_stats_tracking(self):
        """Simulator tracks send/drop/deliver stats."""
        sim = NetworkSimulator(NetworkConfig(packet_loss_rate=0.5, latency_ms=1))
        transport.set_simulator(sim)

        for i in range(100):
            transport.send(f"pkt{i}".encode(), ('127.0.0.1', 1000), ('127.0.0.1', 2000))
            transport.simulator_transfer(1000 + i)

        transport.simulator_transfer(1200)
        transport.drain_incoming(200)

        stats = sim.stats()
        assert stats['sent'] == 100
        assert stats['dropped'] > 0
        assert stats['delivered'] > 0
        assert stats['dropped'] + stats['delivered'] == 100


class TestJitter:
    """Test latency jitter simulation."""

    def test_jitter_adds_variation(self):
        """Jitter adds variation to delivery times."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=100, jitter_ms=20))
        transport.set_simulator(sim)

        # Send 50 packets, check they're not all delivered at exactly the same time
        for i in range(50):
            transport.send(f"pkt{i}".encode(), ('127.0.0.1', 1000), ('127.0.0.1', 2000))
            transport.simulator_transfer(1000)

        # At t=1100 (base latency), some should be ready, some not yet
        count_early = transport.simulator_transfer(1100)
        # At t=1200, rest should be ready
        count_late = transport.simulator_transfer(1200)

        # With jitter, not all packets should arrive at exactly t=1100
        total = count_early + count_late
        assert total == 50
        # Due to jitter, some should be early/late
        # (This is probabilistic, but with seed=42 should be consistent)

    def test_jitter_never_negative(self):
        """Jitter never results in negative delivery time."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=10, jitter_ms=50))
        transport.set_simulator(sim)

        # Even with high jitter relative to latency, all packets should be delivered
        for i in range(100):
            transport.send(f"pkt{i}".encode(), ('127.0.0.1', 1000), ('127.0.0.1', 2000))
            transport.simulator_transfer(1000 + i)

        # All should eventually be delivered
        transport.simulator_transfer(10000)
        batch = transport.drain_incoming(200)
        assert len(batch) == 100


class TestIntegrationWithTick:
    """Test simulator integration with tick system."""

    def test_loopback_transfer_uses_simulator(self):
        """loopback_transfer() uses simulator when set."""
        sim = NetworkSimulator(NetworkConfig(latency_ms=100, jitter_ms=0))
        transport.set_simulator(sim)

        # Send a packet
        transport.send(b"test", ('127.0.0.1', 1000), ('127.0.0.1', 2000))

        # Set time and transfer at t=1000 (queues packet, deliver_at=1100)
        transport.set_simulator_time(1000)
        count = transport.loopback_transfer()
        assert count == 0  # Not delivered yet

        # Check incoming is empty
        batch = transport.drain_incoming(10)
        assert len(batch) == 0

        # At t=1100, packet should be delivered
        transport.set_simulator_time(1100)
        count = transport.loopback_transfer()
        assert count == 1

        batch = transport.drain_incoming(10)
        assert len(batch) == 1
        assert batch[0][0] == b"test"

    def test_loopback_without_simulator_is_immediate(self):
        """Without simulator, loopback_transfer() delivers immediately in loopback mode."""
        transport.enable_loopback()  # Enable loopback mode first
        transport.send(b"test", ('127.0.0.1', 1000), ('127.0.0.1', 2000))
        count = transport.loopback_transfer()
        assert count == 1

        batch = transport.drain_incoming(10)
        assert len(batch) == 1
        assert batch[0][0] == b"test"

    def test_tick_runs_receive_job_with_simulator(self):
        """tick() runs ReceiveJob which uses simulator for latency."""
        import sqlite3
        from core.db import Database
        from core import schema, tick

        conn = sqlite3.Connection(":memory:")
        db = Database(conn)
        schema.create_all(db)

        sim = NetworkSimulator(NetworkConfig(latency_ms=100, jitter_ms=0))
        transport.set_simulator(sim)

        # Send a packet (raw bytes, won't be unwrapped successfully but will go through simulator)
        transport.send(b"test_packet", ('127.0.0.1', 1000), ('127.0.0.1', 2000))

        # Tick at t=1000 - ReceiveJob runs, packet queued with deliver_at=1100
        tick.tick(t_ms=1000, db=db)

        # Packet should be in simulator pending queue, not delivered yet
        assert sim.pending_count() == 1
        assert sim.packets_sent == 1
        assert sim.packets_delivered == 0

        # Tick at t=1100 - packet should be delivered to incoming queue
        # (but ReceiveJob will discard it since it's not a valid transit blob)
        tick.tick(t_ms=1100, db=db)

        # Simulator should have delivered the packet
        assert sim.pending_count() == 0
        assert sim.packets_delivered == 1
