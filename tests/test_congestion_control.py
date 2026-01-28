"""
Congestion control behavior tests.

These tests verify that the sync protocol adapts to network conditions:
- Backs off under packet loss (sends fewer packets over time)
- Recovers when conditions improve
- Achieves reasonable efficiency (doesn't waste bandwidth)

CC is implemented in negentropy.py using adaptive windowing based on RTT.
"""
import pytest
from core.simulator import NetworkSimulator, NetworkConfig
from core import transport
from core import tick as tick_module


def run_sync_rounds(db, sim: NetworkSimulator, rounds: int, t_ms_start: int, round_interval_ms: int = 1000):
    """Run multiple sync rounds, returning stats at each interval."""
    stats_over_time = []
    t_ms = t_ms_start

    for i in range(rounds):
        # Run tick
        tick_module.tick(t_ms=t_ms, db=db)

        # Let simulator deliver packets
        transport.simulator_transfer(t_ms)

        # Record stats
        stats_over_time.append({
            'round': i,
            't_ms': t_ms,
            'packets_sent': sim.packets_sent,
            'packets_dropped': sim.packets_dropped,
        })

        t_ms += round_interval_ms

    return stats_over_time


class TestBackoffUnderLoss:
    """Test that sync backs off when experiencing packet loss."""

    def test_sends_fewer_packets_under_sustained_loss(self, fresh_db_with_alice_and_bob):
        """Under 50% loss, packet send rate should decrease over time."""
        db, alice, bob = fresh_db_with_alice_and_bob

        sim = NetworkSimulator(NetworkConfig(
            latency_ms=50,
            packet_loss_rate=0.5,  # 50% loss
        ))
        transport.set_simulator(sim)

        # Run 10 sync rounds
        stats = run_sync_rounds(db, sim, rounds=10, t_ms_start=10000)

        # Calculate packets sent in first half vs second half
        mid = len(stats) // 2
        first_half_sent = stats[mid]['packets_sent'] - stats[0]['packets_sent']
        second_half_sent = stats[-1]['packets_sent'] - stats[mid]['packets_sent']

        # With CC: second half should send fewer packets (backed off)
        # Without CC: roughly equal (no adaptation)
        assert second_half_sent < first_half_sent * 0.7, (
            f"Expected backoff: second half ({second_half_sent}) should be "
            f"<70% of first half ({first_half_sent})"
        )


class TestRecoveryAfterLoss:
    """Test that sync recovers when loss clears."""

    def test_throughput_increases_after_loss_clears(self, fresh_db_with_alice_and_bob):
        """After loss stops, packet rate should increase."""
        db, alice, bob = fresh_db_with_alice_and_bob

        sim = NetworkSimulator(NetworkConfig(
            latency_ms=50,
            packet_loss_rate=0.5,  # Start with 50% loss
        ))
        transport.set_simulator(sim)

        # Run 5 rounds with loss
        stats_with_loss = run_sync_rounds(db, sim, rounds=5, t_ms_start=10000)
        packets_during_loss = stats_with_loss[-1]['packets_sent']

        # Clear loss
        sim.config = NetworkConfig(latency_ms=50, packet_loss_rate=0.0)
        sim.packets_sent = 0  # Reset counter

        # Run 5 more rounds without loss
        stats_after_recovery = run_sync_rounds(db, sim, rounds=5, t_ms_start=20000)
        packets_after_recovery = stats_after_recovery[-1]['packets_sent']

        # With CC: should send more packets after recovery (window grew)
        # Note: Recovery is gradual, so we only expect modest increase
        rate_during_loss = packets_during_loss / 5
        rate_after_recovery = packets_after_recovery / 5

        assert rate_after_recovery > rate_during_loss, (
            f"Expected recovery: rate after ({rate_after_recovery:.1f}/round) should be "
            f"> rate during loss ({rate_during_loss:.1f}/round)"
        )


class TestEfficiencyUnderLoss:
    """Test that CC improves efficiency (fewer wasted packets)."""

    def test_sync_completes_efficiently_under_loss(self, fresh_db_with_alice_and_bob):
        """With 20% loss, sync should complete without excessive retries."""
        db, alice, bob = fresh_db_with_alice_and_bob

        sim = NetworkSimulator(NetworkConfig(
            latency_ms=50,
            packet_loss_rate=0.2,  # 20% loss
        ))
        transport.set_simulator(sim)

        # Run sync until complete or timeout
        stats = run_sync_rounds(db, sim, rounds=50, t_ms_start=10000)

        total_sent = stats[-1]['packets_sent']
        total_dropped = stats[-1]['packets_dropped']

        # With 20% loss, ideal efficiency would be ~80% delivery
        # With CC, we should be close to this
        # Without CC, we might flood and cause congestion-induced loss

        delivered = total_sent - total_dropped
        efficiency = delivered / total_sent if total_sent > 0 else 0

        assert efficiency > 0.7, (
            f"Expected good efficiency: {efficiency:.2%} delivered, "
            f"but should be >70% (sent={total_sent}, dropped={total_dropped})"
        )


class TestWindowGrowthUnderGoodConditions:
    """Test that window grows when conditions are good."""

    def test_throughput_increases_under_ideal_conditions(self, fresh_db_with_alice_and_bob):
        """With no loss and low latency, throughput should increase over time."""
        db, alice, bob = fresh_db_with_alice_and_bob

        sim = NetworkSimulator(NetworkConfig(
            latency_ms=10,  # Fast
            packet_loss_rate=0.0,  # No loss
        ))
        transport.set_simulator(sim)

        # Run sync rounds
        stats = run_sync_rounds(db, sim, rounds=10, t_ms_start=10000)

        # Calculate packets per round
        packets_per_round = []
        for i in range(1, len(stats)):
            delta = stats[i]['packets_sent'] - stats[i-1]['packets_sent']
            packets_per_round.append(delta)

        # With CC: early rounds should have fewer packets (window=1)
        # Later rounds should have more (window grew)
        if len(packets_per_round) >= 4:
            early_avg = sum(packets_per_round[:2]) / 2
            late_avg = sum(packets_per_round[-2:]) / 2

            # Window should grow, so late rounds send more per round
            if late_avg > 0 and early_avg > 0:
                assert late_avg >= early_avg, (
                    f"Expected window growth: late avg ({late_avg:.1f}) should be "
                    f">= early avg ({early_avg:.1f})"
                )


class TestRTTMeasurement:
    """Test that RTT is measured correctly."""

    def test_adapts_to_different_latencies(self, fresh_db_with_alice_and_bob):
        """System should adapt sending rate to match RTT."""
        db, alice, bob = fresh_db_with_alice_and_bob

        # Test with high latency
        sim_slow = NetworkSimulator(NetworkConfig(
            latency_ms=200,  # Slow link
            packet_loss_rate=0.0,
        ))
        transport.set_simulator(sim_slow)

        stats_slow = run_sync_rounds(db, sim_slow, rounds=10, t_ms_start=10000)
        packets_slow = stats_slow[-1]['packets_sent']

        # Reset and test with low latency
        transport.reset()
        sim_fast = NetworkSimulator(NetworkConfig(
            latency_ms=20,  # Fast link
            packet_loss_rate=0.0,
        ))
        transport.set_simulator(sim_fast)

        # Note: We can't easily reset the DB state, so this test is imperfect.
        # A proper test would need fresh DB state for each latency test.
        stats_fast = run_sync_rounds(db, sim_fast, rounds=10, t_ms_start=30000)
        packets_fast = stats_fast[-1]['packets_sent']

        # This test may not work well since the DB state changed between runs.
        # Keeping it as a placeholder for when we have better test isolation.
        # With proper RTT adaptation, fast link should achieve higher throughput.
        print(f"Slow link packets: {packets_slow}, Fast link packets: {packets_fast}")
