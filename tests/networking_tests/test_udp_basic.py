"""
Basic UDP networking tests.

These tests verify the UDP transport layer works correctly
before integrating with the full sync system.
"""
import time
from tests.networking_tests.conftest import (
    Client, UDPSocket, tick_all, assert_eventually_multi
)


def test_udp_packet_roundtrip(create_client):
    """Two UDP sockets can send packets to each other."""
    # Create two clients
    alice = create_client("alice")
    bob = create_client("bob")

    # They know each other's addresses
    alice.network.add_peer("bob", ("127.0.0.1", bob.network.port))
    bob.network.add_peer("alice", ("127.0.0.1", alice.network.port))

    # Alice sends to Bob
    alice.network.send("bob", b"hello from alice")

    # Give packet time to arrive
    time.sleep(0.05)

    # Bob should have received it
    packets = bob.network.drain()
    assert len(packets) == 1
    data, addr = packets[0]
    assert data == b"hello from alice"
    assert addr[1] == alice.network.port  # From Alice's port

    # Bob sends back to Alice
    bob.network.send("alice", b"hello from bob")
    time.sleep(0.05)

    # Alice should have received it
    packets = alice.network.drain()
    assert len(packets) == 1
    data, addr = packets[0]
    assert data == b"hello from bob"
    assert addr[1] == bob.network.port


def test_multiple_packets(create_client):
    """Multiple packets queue up correctly."""
    alice = create_client("alice")
    bob = create_client("bob")

    bob.network.add_peer("alice", ("127.0.0.1", alice.network.port))

    # Bob sends multiple packets
    for i in range(10):
        bob.network.send("alice", f"packet {i}".encode())

    time.sleep(0.1)

    # Alice receives all of them
    packets = alice.network.drain()
    assert len(packets) == 10

    # Verify order (UDP doesn't guarantee order, but localhost usually preserves it)
    contents = [p[0].decode() for p in packets]
    assert contents == [f"packet {i}" for i in range(10)]


def test_separate_databases(create_client):
    """Each client has its own isolated database."""
    from events.identity import user

    alice = create_client("alice")
    bob = create_client("bob")

    # Alice creates a network in her database
    alice_result = user.new_network(name='Alice', t_ms=1000, db=alice.db)
    alice.peer_id = alice_result['peer_id']
    alice.network_id = alice_result['network_id']
    alice.db.commit()

    # Bob creates a DIFFERENT network in his database
    bob_result = user.new_network(name='Bob', t_ms=1000, db=bob.db)
    bob.peer_id = bob_result['peer_id']
    bob.network_id = bob_result['network_id']
    bob.db.commit()

    # They should have different network IDs (different databases, different keys)
    assert alice.network_id != bob.network_id

    # Alice's DB should NOT see Bob's network
    alice_networks = alice.db.query("SELECT network_id FROM networks")
    assert len(alice_networks) == 1
    assert alice_networks[0]['network_id'] == alice.network_id

    # Bob's DB should NOT see Alice's network
    bob_networks = bob.db.query("SELECT network_id FROM networks")
    assert len(bob_networks) == 1
    assert bob_networks[0]['network_id'] == bob.network_id


def test_client_tick_runs(create_client):
    """Client tick() runs without error."""
    alice = create_client("alice")

    # Create a network first
    from events.identity import user
    result = user.new_network(name='Alice', t_ms=1000, db=alice.db)
    alice.peer_id = result['peer_id']
    alice.db.commit()

    # Tick should work
    alice.tick(t_ms=2000)
    alice.tick(t_ms=2100)
    alice.tick(t_ms=2200)

    # No assertion needed - just verify it doesn't crash
