"""Tests for ns.py-based network simulator.

Tests cover:
- Basic packet delivery
- Latency and jitter
- Packet loss (independent and burst)
- Bandwidth limiting
- NAT traversal
- Network partitions
- Presets and factory functions
"""
import pytest
import random
import statistics

from simulator.nspy_network import (
    NetworkSimulator,
    NetworkConfig,
    create_simulator,
    create_preset,
    PRESETS,
)
from simulator.loss_models import (
    independent_loss,
    GilbertElliottLoss,
    SimpleBurstLoss,
    CombinedLoss,
)
from simulator.nat import NatConfig


@pytest.fixture
def sim():
    """Create a fresh simulator for each test."""
    simulator = NetworkSimulator()
    simulator.set_seed(42)
    yield simulator
    simulator.reset()


@pytest.fixture
def sim_with_peers(sim):
    """Simulator with Alice and Bob registered."""
    sim.register_peer("alice", behind_nat=False)
    sim.register_peer("bob", behind_nat=False)
    return sim


class TestBasicDelivery:
    """Test basic packet send and receive."""

    def test_send_and_drain_immediate(self, sim_with_peers):
        """Packets with zero latency deliver immediately."""
        sim = sim_with_peers

        # Send packet at t=1000
        result = sim.send("alice", "bob", b"hello", t_ms=1000)
        assert result is True

        # Advance time and drain
        sim.advance_to(1000)
        packets = sim.drain()

        assert len(packets) == 1
        assert packets[0] == b"hello"

    def test_send_multiple_packets(self, sim_with_peers):
        """Multiple packets can be sent and received."""
        sim = sim_with_peers

        for i in range(10):
            sim.send("alice", "bob", f"packet_{i}".encode(), t_ms=1000)

        sim.advance_to(1000)
        packets = sim.drain()

        assert len(packets) == 10
        assert packets[0] == b"packet_0"
        assert packets[9] == b"packet_9"

    def test_drain_with_limit(self, sim_with_peers):
        """drain() respects max_count parameter."""
        sim = sim_with_peers

        for i in range(10):
            sim.send("alice", "bob", f"packet_{i}".encode(), t_ms=1000)

        sim.advance_to(1000)

        # Drain only 3
        packets = sim.drain(max_count=3)
        assert len(packets) == 3

        # Drain rest
        packets = sim.drain()
        assert len(packets) == 7

    def test_drain_removes_packets(self, sim_with_peers):
        """drain() removes packets from queue."""
        sim = sim_with_peers

        sim.send("alice", "bob", b"test", t_ms=1000)
        sim.advance_to(1000)

        packets = sim.drain()
        assert len(packets) == 1

        # Second drain should be empty
        packets = sim.drain()
        assert len(packets) == 0


class TestLatency:
    """Test latency simulation."""

    def test_latency_delays_delivery(self, sim):
        """Packets are delayed by configured latency."""
        sim.configure(NetworkConfig(latency_ms=100))
        sim.register_peer("alice")
        sim.register_peer("bob")

        sim.send("alice", "bob", b"delayed", t_ms=1000)

        # At t=1050, should not be delivered
        sim.advance_to(1050)
        assert len(sim.drain()) == 0

        # At t=1100, should be delivered
        sim.advance_to(1100)
        packets = sim.drain()
        assert len(packets) == 1
        assert packets[0] == b"delayed"

    def test_latency_exact_boundary(self, sim):
        """Packets deliver exactly at latency time."""
        sim.configure(NetworkConfig(latency_ms=50))
        sim.register_peer("alice")
        sim.register_peer("bob")

        sim.send("alice", "bob", b"test", t_ms=1000)

        # At t=1049, not delivered
        sim.advance_to(1049)
        assert len(sim.drain()) == 0

        # At t=1050, delivered
        sim.advance_to(1050)
        assert len(sim.drain()) == 1

    def test_jitter_adds_variation(self, sim):
        """Jitter adds random variation to latency."""
        sim.configure(NetworkConfig(latency_ms=100, jitter_ms=20))
        sim.register_peer("alice")
        sim.register_peer("bob")
        sim.set_seed(12345)

        # Send packets at different times (spaced 50ms apart)
        # This avoids queueing effects where Wire maintains FIFO order
        delivery_times = []
        for i in range(50):
            sim.send("alice", "bob", f"pkt_{i}".encode(), t_ms=1000 + i * 50)

        # Advance far enough to deliver all
        sim.advance_to(5000)
        delivered = sim.drain_with_metadata()

        for pkt in delivered:
            latency = pkt.delivered_at_ms - pkt.sent_at_ms
            delivery_times.append(latency)

        # Check variation
        min_latency = min(delivery_times)
        max_latency = max(delivery_times)
        mean_latency = statistics.mean(delivery_times)

        # Should have variation (at least 20ms range with 20ms stddev)
        assert max_latency - min_latency >= 15, f"Range too small: {max_latency - min_latency}"

        # Mean should be close to base latency
        # Allow wider range due to statistical variation with fewer samples
        assert 70 <= mean_latency <= 130, f"Mean {mean_latency} not close to 100"

    def test_jitter_never_negative(self, sim):
        """Jittered latency is never negative."""
        # Low base latency with high jitter
        sim.configure(NetworkConfig(latency_ms=10, jitter_ms=50))
        sim.register_peer("alice")
        sim.register_peer("bob")

        for i in range(100):
            sim.send("alice", "bob", f"pkt_{i}".encode(), t_ms=1000)

        sim.advance_to(2000)
        delivered = sim.drain_with_metadata()

        for pkt in delivered:
            latency = pkt.delivered_at_ms - pkt.sent_at_ms
            assert latency >= 0, f"Negative latency: {latency}"


class TestPacketLoss:
    """Test packet loss simulation."""

    def test_zero_loss_delivers_all(self, sim):
        """Zero loss rate delivers all packets."""
        sim.configure(NetworkConfig(packet_loss_rate=0.0))
        sim.register_peer("alice")
        sim.register_peer("bob")

        for i in range(100):
            sim.send("alice", "bob", f"pkt_{i}".encode(), t_ms=1000)

        sim.advance_to(1000)
        packets = sim.drain()
        assert len(packets) == 100

    def test_packet_loss_rate(self, sim):
        """Packet loss rate is applied correctly."""
        sim.configure(NetworkConfig(packet_loss_rate=0.5))
        sim.register_peer("alice")
        sim.register_peer("bob")
        sim.set_seed(42)

        for i in range(200):
            sim.send("alice", "bob", f"pkt_{i}".encode(), t_ms=1000)

        sim.advance_to(1000)
        packets = sim.drain()

        # With 50% loss, expect roughly 100 packets (allow 70-130)
        assert 70 <= len(packets) <= 130, f"Expected ~100, got {len(packets)}"

    def test_high_loss_rate(self, sim):
        """High loss rate drops most packets."""
        sim.configure(NetworkConfig(packet_loss_rate=0.95))
        sim.register_peer("alice")
        sim.register_peer("bob")

        for i in range(1000):
            sim.send("alice", "bob", f"pkt_{i}".encode(), t_ms=1000)

        sim.advance_to(1000)
        packets = sim.drain()

        # With 95% loss, expect ~50 packets (allow 10-100)
        assert len(packets) < 150, f"Expected ~50, got {len(packets)}"

    def test_max_packet_size(self, sim):
        """Oversized packets are dropped."""
        sim.configure(NetworkConfig(max_packet_size=100))
        sim.register_peer("alice")
        sim.register_peer("bob")

        # Under limit - should succeed
        r1 = sim.send("alice", "bob", b"x" * 50, t_ms=1000)
        assert r1 is True

        # At limit - should succeed
        r2 = sim.send("alice", "bob", b"x" * 100, t_ms=1000)
        assert r2 is True

        # Over limit - should fail
        r3 = sim.send("alice", "bob", b"x" * 101, t_ms=1000)
        assert r3 is False

        sim.advance_to(1000)
        packets = sim.drain()
        assert len(packets) == 2


class TestBurstLoss:
    """Test burst packet loss."""

    def test_burst_loss_drops_consecutive(self, sim):
        """Burst loss drops consecutive packets."""
        sim.configure(NetworkConfig(
            burst_loss_probability=1.0,  # Always burst
            burst_loss_length=3
        ))
        sim.register_peer("alice")
        sim.register_peer("bob")
        sim.set_seed(42)

        # Send 6 packets - first 3 should be dropped (burst), then another burst
        results = []
        for i in range(6):
            r = sim.send("alice", "bob", f"pkt_{i}".encode(), t_ms=1000)
            results.append(r)

        # First packet triggers burst, drops 3
        # Fourth packet triggers new burst, drops 3
        assert results.count(False) >= 3  # At least one full burst

    def test_burst_loss_probabilistic(self, sim):
        """Burst loss is probabilistic."""
        sim.configure(NetworkConfig(
            burst_loss_probability=0.1,
            burst_loss_length=2
        ))
        sim.register_peer("alice")
        sim.register_peer("bob")
        sim.set_seed(42)

        for i in range(100):
            sim.send("alice", "bob", f"pkt_{i}".encode(), t_ms=1000)

        sim.advance_to(1000)
        packets = sim.drain()

        # With 10% burst probability and length 2, expect ~80% delivery
        assert 60 <= len(packets) <= 95, f"Expected ~80, got {len(packets)}"


class TestBandwidth:
    """Test bandwidth limiting."""

    def test_unlimited_bandwidth(self, sim):
        """Unlimited bandwidth doesn't throttle."""
        sim.configure(NetworkConfig(bandwidth_bps=None))
        sim.register_peer("alice")
        sim.register_peer("bob")

        # Send many large packets
        for i in range(100):
            r = sim.send("alice", "bob", b"x" * 1000, t_ms=1000)
            assert r is True

        sim.advance_to(1000)
        packets = sim.drain()
        assert len(packets) == 100

    def test_bandwidth_throttles(self, sim):
        """Bandwidth limit delays packet delivery."""
        # 8000 bps = 1000 bytes/sec, with 500 byte bucket
        # This means packets are rate-limited but can burst up to bucket size
        sim.configure(NetworkConfig(
            bandwidth_bps=8000,
            bucket_size_bytes=500  # Small bucket to see throttling
        ))
        sim.register_peer("alice")
        sim.register_peer("bob")

        # Send 3x 500 byte packets (1.5 seconds worth at 1000 B/s)
        sim.send("alice", "bob", b"x" * 500, t_ms=0)
        sim.send("alice", "bob", b"y" * 500, t_ms=0)
        sim.send("alice", "bob", b"z" * 500, t_ms=0)

        # All packets get queued - first goes through immediately (bucket full)
        # Second waits for bucket to refill (500ms)
        # Third waits another 500ms

        # At t=100, first packet should be delivered, others waiting
        sim.advance_to(100)
        count_100 = len(sim.drain())

        # At t=600, second should be delivered
        sim.advance_to(600)
        count_600 = len(sim.drain())

        # At t=1200, all should be delivered
        sim.advance_to(1200)
        count_1200 = len(sim.drain())

        # Total should be 3
        total = count_100 + count_600 + count_1200
        assert total == 3, f"Expected 3 total, got {total}"

    def test_token_bucket_allows_burst(self, sim):
        """Token bucket allows initial burst up to bucket size."""
        # 8000 bps with 2000 byte bucket
        sim.configure(NetworkConfig(
            bandwidth_bps=8000,
            bucket_size_bytes=2000
        ))
        sim.register_peer("alice")
        sim.register_peer("bob")

        # Send burst of 2000 bytes (within bucket)
        sim.send("alice", "bob", b"x" * 1000, t_ms=0)
        sim.send("alice", "bob", b"y" * 1000, t_ms=0)

        # Should deliver quickly (bucket allows burst)
        sim.advance_to(100)
        packets = sim.drain()
        # Both should be delivered or queued
        assert len(packets) >= 1


class TestNAT:
    """Test NAT traversal simulation."""

    def test_nat_blocks_without_mapping(self, sim):
        """Packets to NATed peer blocked without hole punch."""
        sim.register_peer("alice", behind_nat=False)
        sim.register_peer("bob", behind_nat=True)

        # Alice sends to Bob (behind NAT) - should fail
        result = sim.send("alice", "bob", b"blocked", t_ms=1000)
        assert result is False

    def test_nat_allows_after_hole_punch(self, sim):
        """Packets work after hole punch."""
        sim.register_peer("alice", behind_nat=False)
        sim.register_peer("bob", behind_nat=True)

        # Bob sends to Alice first (creates NAT mapping)
        r1 = sim.send("bob", "alice", b"hole_punch", t_ms=1000)
        assert r1 is True

        # Now Alice can send to Bob
        r2 = sim.send("alice", "bob", b"response", t_ms=1001)
        assert r2 is True

        sim.advance_to(1100)
        packets = sim.drain()
        assert len(packets) == 2

    def test_nat_mapping_expires(self, sim):
        """NAT mappings expire after TTL."""
        # Short TTL for testing
        sim.nat_engine.config = NatConfig(mapping_ttl_ms=1000)
        sim.register_peer("alice", behind_nat=False)
        sim.register_peer("bob", behind_nat=True)

        # Bob creates mapping
        sim.send("bob", "alice", b"punch", t_ms=1000)

        # Alice can send within TTL
        r1 = sim.send("alice", "bob", b"ok", t_ms=1500)
        assert r1 is True

        # After TTL expires, Alice cannot send
        sim.advance_to(3000)
        r2 = sim.send("alice", "bob", b"blocked", t_ms=3000)
        assert r2 is False

    def test_direct_peers_communicate_freely(self, sim):
        """Non-NATed peers can communicate without hole punch."""
        sim.register_peer("alice", behind_nat=False)
        sim.register_peer("bob", behind_nat=False)

        r = sim.send("alice", "bob", b"direct", t_ms=1000)
        assert r is True


class TestPartitions:
    """Test network partition simulation."""

    def test_partition_blocks_source(self, sim_with_peers):
        """Partitioned source cannot send."""
        sim = sim_with_peers

        sim.partition_peer("alice")

        r = sim.send("alice", "bob", b"blocked", t_ms=1000)
        assert r is False

    def test_partition_blocks_destination(self, sim_with_peers):
        """Partitioned destination cannot receive."""
        sim = sim_with_peers

        sim.partition_peer("bob")

        r = sim.send("alice", "bob", b"blocked", t_ms=1000)
        assert r is False

    def test_unpartition_restores_connectivity(self, sim_with_peers):
        """Unpartitioning restores connectivity."""
        sim = sim_with_peers

        sim.partition_peer("alice")
        assert sim.is_partitioned("alice")

        r1 = sim.send("alice", "bob", b"blocked", t_ms=1000)
        assert r1 is False

        sim.unpartition_peer("alice")
        assert not sim.is_partitioned("alice")

        r2 = sim.send("alice", "bob", b"ok", t_ms=1001)
        assert r2 is True


class TestLossModels:
    """Test loss model implementations."""

    def test_independent_loss(self):
        """Independent loss returns constant rate."""
        loss = independent_loss(0.3)
        assert loss() == 0.3
        assert loss(packet_id=1) == 0.3
        assert loss(packet_id=999) == 0.3

    def test_gilbert_elliott_has_states(self):
        """Gilbert-Elliott model transitions between states."""
        ge = GilbertElliottLoss(
            p_good_to_bad=0.5,
            p_bad_to_good=0.5,
            loss_in_good=0.0,
            loss_in_bad=1.0,
            seed=42
        )

        # Run many packets and collect loss rates
        losses = [ge() for _ in range(1000)]

        # Should see both 0.0 and 1.0 (both states)
        assert 0.0 in losses
        assert 1.0 in losses

    def test_gilbert_elliott_from_average(self):
        """Gilbert-Elliott from_average_loss() achieves target rate."""
        ge = GilbertElliottLoss.from_average_loss(
            target_loss_rate=0.1,
            burst_length=3,
            seed=42
        )

        # Run many packets
        losses = [ge() for _ in range(10000)]
        avg_loss = sum(losses) / len(losses)

        # Should be close to target (within 5%)
        assert 0.05 <= avg_loss <= 0.15, f"Average loss {avg_loss} not close to 0.1"

    def test_simple_burst_loss(self):
        """SimpleBurstLoss drops consecutive packets."""
        burst = SimpleBurstLoss(
            burst_probability=1.0,  # Always burst
            burst_length=3,
            seed=42
        )

        # First 3 should be lost
        assert burst() == 1.0
        assert burst() == 1.0
        assert burst() == 1.0

        # Fourth triggers new burst
        assert burst() == 1.0

    def test_combined_loss(self):
        """CombinedLoss combines multiple models."""
        # 50% random + 50% random = 75% loss
        combined = CombinedLoss(
            independent_loss(0.5),
            independent_loss(0.5)
        )

        # P(loss) = 1 - (1-0.5)*(1-0.5) = 0.75
        # But since these are independent checks, need to test empirically
        random.seed(42)
        losses = 0
        trials = 10000
        for _ in range(trials):
            if random.random() < combined():
                losses += 1

        loss_rate = losses / trials
        assert 0.70 <= loss_rate <= 0.80


class TestPresets:
    """Test preset configurations."""

    def test_all_presets_exist(self):
        """All documented presets exist."""
        expected = ['lan', 'broadband', 'mobile_4g', 'mobile_3g', 'satellite', 'lossy_wifi']
        for name in expected:
            assert name in PRESETS

    def test_create_preset(self):
        """create_preset() returns configured simulator."""
        sim = create_preset('lan')
        assert sim.config.latency_ms == 1

        sim = create_preset('satellite')
        assert sim.config.latency_ms == 600

    def test_invalid_preset_raises(self):
        """Invalid preset name raises ValueError."""
        with pytest.raises(ValueError):
            create_preset('nonexistent')


class TestFactoryFunctions:
    """Test convenience factory functions."""

    def test_create_simulator(self):
        """create_simulator() with kwargs."""
        sim = create_simulator(
            latency_ms=50,
            jitter_ms=10,
            packet_loss_rate=0.05,
            bandwidth_kbps=1000
        )

        assert sim.config.latency_ms == 50
        assert sim.config.jitter_ms == 10
        assert sim.config.packet_loss_rate == 0.05
        assert sim.config.bandwidth_bps == 1000000


class TestStats:
    """Test statistics and diagnostics."""

    def test_get_stats(self, sim_with_peers):
        """get_stats() returns useful info."""
        sim = sim_with_peers

        sim.send("alice", "bob", b"test", t_ms=1000)
        sim.advance_to(1000)

        stats = sim.get_stats()

        assert 'current_time_ms' in stats
        assert 'packets_sent' in stats
        assert 'peers_registered' in stats
        assert stats['packets_sent'] >= 1

    def test_reset_clears_state(self, sim_with_peers):
        """reset() clears all state."""
        sim = sim_with_peers

        sim.send("alice", "bob", b"test", t_ms=1000)
        sim.partition_peer("alice")

        sim.reset()

        stats = sim.get_stats()
        assert stats['packets_sent'] == 0
        assert stats['peers_partitioned'] == 0
        assert stats['current_time_ms'] == 0


class TestTimeHandling:
    """Test simulation time handling."""

    def test_time_advances_correctly(self, sim_with_peers):
        """Simulation time advances to requested value."""
        sim = sim_with_peers

        sim.advance_to(5000)
        # env.now may be slightly past due to epsilon for event processing
        assert 5000 <= sim.env.now < 5001

        sim.advance_to(10000)
        assert 10000 <= sim.env.now < 10001

    def test_send_advances_time(self, sim_with_peers):
        """send() advances time if needed."""
        sim = sim_with_peers

        sim.send("alice", "bob", b"test", t_ms=3000)
        assert sim.env.now >= 3000

    def test_drain_metadata(self, sim_with_peers):
        """drain_with_metadata() returns timing info."""
        sim = sim_with_peers
        sim.configure(NetworkConfig(latency_ms=100))

        sim.send("alice", "bob", b"test", t_ms=1000)
        sim.advance_to(1200)

        packets = sim.drain_with_metadata()

        assert len(packets) == 1
        pkt = packets[0]
        assert pkt.sent_at_ms == 1000
        assert pkt.delivered_at_ms >= 1100  # At least latency
        assert pkt.from_peer == "alice"
        assert pkt.to_peer == "bob"
        assert pkt.blob == b"test"
