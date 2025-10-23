"""Tests for network simulator: latency, packet loss, time-based delivery."""
import sqlite3
import random
import pytest
from db import Database
import schema
from db import create_unsafe_db
import queues
import network_config


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
    random.seed(42)  # Deterministic randomness for testing
    yield
    network_config.reset_network_config()


def test_incoming_with_zero_latency(db):
    """Test that packets with zero latency deliver immediately."""
    # Setup: default config (zero latency, zero loss)
    unsafedb = create_unsafe_db(db)

    # Add packet at t=1000
    queues.incoming.add(b"test_packet", t_ms=1000, unsafedb=unsafedb)
    db.commit()

    # Should be deliverable immediately at t=1000 (or any time >= 1000)
    received = queues.incoming.drain(10, current_time_ms=1000, unsafedb=unsafedb)
    assert len(received) == 1
    assert received[0] == b"test_packet"


def test_incoming_with_latency(db):
    """Test that latency delays packet delivery."""
    # Setup: 100ms latency
    network_config.set_network_config(network_config.NetworkConfig(latency_ms=100))
    unsafedb = create_unsafe_db(db)

    # Add packet at t=1000
    queues.incoming.add(b"test_packet", t_ms=1000, unsafedb=unsafedb)
    db.commit()

    # Should NOT be deliverable at t=1050 (before delivery window)
    received = queues.incoming.drain(10, current_time_ms=1050, unsafedb=unsafedb)
    assert len(received) == 0

    # Should be deliverable at t=1100 (after delivery window)
    received = queues.incoming.drain(10, current_time_ms=1100, unsafedb=unsafedb)
    assert len(received) == 1
    assert received[0] == b"test_packet"


def test_incoming_with_exact_latency_boundary(db):
    """Test that packets deliver exactly at deliver_at time."""
    # Setup: 50ms latency
    network_config.set_network_config(network_config.NetworkConfig(latency_ms=50))
    unsafedb = create_unsafe_db(db)

    # Add packet at t=1000, should deliver at t=1050
    queues.incoming.add(b"test_packet", t_ms=1000, unsafedb=unsafedb)
    db.commit()

    # At t=1049, not yet deliverable
    received = queues.incoming.drain(10, current_time_ms=1049, unsafedb=unsafedb)
    assert len(received) == 0

    # At t=1050, exactly deliverable
    received = queues.incoming.drain(10, current_time_ms=1050, unsafedb=unsafedb)
    assert len(received) == 1
    assert received[0] == b"test_packet"


def test_packet_loss_rate(db):
    """Test that packet loss rate is applied correctly."""
    # Setup: 50% packet loss
    network_config.set_network_config(network_config.NetworkConfig(packet_loss_rate=0.5))
    unsafedb = create_unsafe_db(db)

    # Send 100 packets
    num_sent = 100
    for i in range(num_sent):
        queues.incoming.add(f"packet_{i}".encode(), t_ms=1000, unsafedb=unsafedb)
    db.commit()

    # With 50% loss, expect roughly 50 packets to survive
    # Allow variance (30-70 packets out of 100)
    received = queues.incoming.drain(num_sent, current_time_ms=1000, unsafedb=unsafedb)
    assert 30 <= len(received) <= 70, f"Expected ~50 packets, got {len(received)}"


def test_packet_loss_zero(db):
    """Test that zero packet loss means all packets are delivered."""
    # Setup: 0% packet loss
    network_config.set_network_config(network_config.NetworkConfig(packet_loss_rate=0.0))
    unsafedb = create_unsafe_db(db)

    # Send 100 packets
    num_sent = 100
    for i in range(num_sent):
        queues.incoming.add(f"packet_{i}".encode(), t_ms=1000, unsafedb=unsafedb)
    db.commit()

    # All packets should be delivered
    received = queues.incoming.drain(num_sent, current_time_ms=1000, unsafedb=unsafedb)
    assert len(received) == num_sent


def test_max_packet_size_enforcement(db):
    """Test that oversized packets are dropped."""
    # Setup: max 100 bytes
    network_config.set_network_config(network_config.NetworkConfig(max_packet_size=100))
    unsafedb = create_unsafe_db(db)

    # Add under-size packet (should succeed)
    queues.incoming.add(b"x" * 50, t_ms=1000, unsafedb=unsafedb)

    # Add exact-size packet (should succeed)
    queues.incoming.add(b"x" * 100, t_ms=1000, unsafedb=unsafedb)

    # Add over-size packet (should be dropped)
    queues.incoming.add(b"x" * 101, t_ms=1000, unsafedb=unsafedb)

    db.commit()

    # Should receive only 2 packets
    received = queues.incoming.drain(10, current_time_ms=1000, unsafedb=unsafedb)
    assert len(received) == 2


def test_latency_and_loss_combined(db):
    """Test latency and packet loss together."""
    # Setup: 50ms latency + 20% loss
    network_config.set_network_config(
        network_config.NetworkConfig(latency_ms=50, packet_loss_rate=0.2)
    )
    unsafedb = create_unsafe_db(db)

    # Send 100 packets at t=1000
    for i in range(100):
        queues.incoming.add(f"packet_{i}".encode(), t_ms=1000, unsafedb=unsafedb)
    db.commit()

    # At t=1049 (before latency), should get nothing
    received = queues.incoming.drain(100, current_time_ms=1049, unsafedb=unsafedb)
    assert len(received) == 0

    # At t=1050 (after latency), should get ~80 packets (100 - 20% loss)
    received = queues.incoming.drain(100, current_time_ms=1050, unsafedb=unsafedb)
    assert 70 <= len(received) <= 90, f"Expected ~80 packets, got {len(received)}"


def test_multiple_batches_with_different_delivery_times(db):
    """Test that multiple packets with different delivery times are sorted correctly."""
    # Setup: 100ms latency
    network_config.set_network_config(network_config.NetworkConfig(latency_ms=100))
    unsafedb = create_unsafe_db(db)

    # Add packets at different times
    queues.incoming.add(b"packet_1", t_ms=1000, unsafedb=unsafedb)  # Delivers at 1100
    queues.incoming.add(b"packet_2", t_ms=1100, unsafedb=unsafedb)  # Delivers at 1200
    queues.incoming.add(b"packet_3", t_ms=1050, unsafedb=unsafedb)  # Delivers at 1150
    db.commit()

    # At t=1100, only packet_1 should be ready
    received = queues.incoming.drain(10, current_time_ms=1100, unsafedb=unsafedb)
    assert len(received) == 1
    assert received[0] == b"packet_1"

    # At t=1150, packet_3 should be ready
    received = queues.incoming.drain(10, current_time_ms=1150, unsafedb=unsafedb)
    assert len(received) == 1
    assert received[0] == b"packet_3"

    # At t=1200, packet_2 should be ready
    received = queues.incoming.drain(10, current_time_ms=1200, unsafedb=unsafedb)
    assert len(received) == 1
    assert received[0] == b"packet_2"


def test_drain_respects_batch_size(db):
    """Test that drain respects the batch_size parameter."""
    unsafedb = create_unsafe_db(db)

    # Add 5 packets
    for i in range(5):
        queues.incoming.add(f"packet_{i}".encode(), t_ms=1000, unsafedb=unsafedb)
    db.commit()

    # Request only 2 packets
    received = queues.incoming.drain(batch_size=2, current_time_ms=1000, unsafedb=unsafedb)
    assert len(received) == 2

    # Request 5 more packets (should get remaining 3)
    received = queues.incoming.drain(batch_size=5, current_time_ms=1000, unsafedb=unsafedb)
    assert len(received) == 3


def test_drain_removes_delivered_packets(db):
    """Test that drain removes packets from the queue."""
    unsafedb = create_unsafe_db(db)

    # Add 3 packets
    for i in range(3):
        queues.incoming.add(f"packet_{i}".encode(), t_ms=1000, unsafedb=unsafedb)
    db.commit()

    # Drain all 3
    received = queues.incoming.drain(10, current_time_ms=1000, unsafedb=unsafedb)
    assert len(received) == 3

    # Try to drain again - should get nothing (packets were removed)
    received = queues.incoming.drain(10, current_time_ms=1000, unsafedb=unsafedb)
    assert len(received) == 0


def test_high_packet_loss(db):
    """Test with very high packet loss rate."""
    # Setup: 99% loss
    network_config.set_network_config(network_config.NetworkConfig(packet_loss_rate=0.99))
    unsafedb = create_unsafe_db(db)

    # Send 1000 packets - expect only ~10 to survive
    for i in range(1000):
        queues.incoming.add(f"packet_{i}".encode(), t_ms=1000, unsafedb=unsafedb)
    db.commit()

    received = queues.incoming.drain(1000, current_time_ms=1000, unsafedb=unsafedb)
    # Allow wide variance but should be much less than 1000
    assert len(received) < 100, f"Expected ~10 packets with 99% loss, got {len(received)}"


def test_network_config_global_state(db):
    """Test that network config is properly global and can be changed."""
    unsafedb = create_unsafe_db(db)

    # Start with default (no latency)
    assert network_config.get_network_config().latency_ms == 0

    queues.incoming.add(b"packet_1", t_ms=1000, unsafedb=unsafedb)
    db.commit()

    received = queues.incoming.drain(10, current_time_ms=1000, unsafedb=unsafedb)
    assert len(received) == 1

    # Change config and add another packet
    network_config.set_network_config(network_config.NetworkConfig(latency_ms=100))
    assert network_config.get_network_config().latency_ms == 100

    queues.incoming.add(b"packet_2", t_ms=2000, unsafedb=unsafedb)
    db.commit()

    # packet_2 should not be ready at t=2000 (needs to wait until t=2100)
    received = queues.incoming.drain(10, current_time_ms=2000, unsafedb=unsafedb)
    assert len(received) == 0

    # But should be ready at t=2100
    received = queues.incoming.drain(10, current_time_ms=2100, unsafedb=unsafedb)
    assert len(received) == 1
    assert received[0] == b"packet_2"
