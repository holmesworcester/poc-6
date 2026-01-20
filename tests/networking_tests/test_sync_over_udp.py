"""
Tests for sync over real UDP transport.

These tests verify that events can be synced between separate client databases
via actual UDP packets on localhost.
"""
import time
from events.identity import user
from core import transport
from tests.networking_tests.conftest import (
    Client, UDPTransport, tick_all, assert_eventually_multi
)


def test_udp_transport_routing(create_client, udp_transport):
    """UDPTransport correctly routes packets between clients."""
    # Create two clients with networks
    alice = create_client("alice")
    bob = create_client("bob")

    # Set up identities
    alice_result = user.new_network(name='Alice', t_ms=1000, db=alice.db)
    alice.peer_id = alice_result['peer_id']
    alice.peer_shared_id = alice_result['peer_shared_id']
    alice.network_id = alice_result['network_id']
    alice.db.commit()

    bob_result = user.new_network(name='Bob', t_ms=1000, db=bob.db)
    bob.peer_id = bob_result['peer_id']
    bob.peer_shared_id = bob_result['peer_shared_id']
    bob.network_id = bob_result['network_id']
    bob.db.commit()

    # Register clients with transport
    udp_transport.register_client(alice)
    udp_transport.register_client(bob)

    # Verify address mapping
    assert bob.peer_shared_id in alice.peer_addresses
    assert alice.peer_shared_id in bob.peer_addresses

    # Test direct UDP send (not through sync system)
    alice.network.send_to_addr(
        (bob.network.host, bob.network.port),
        b"hello from alice"
    )

    time.sleep(0.05)

    # Bob should receive it
    packets = bob.network.drain()
    assert len(packets) == 1
    assert packets[0][0] == b"hello from alice"


def test_transport_callback_intercepts(create_client, udp_transport):
    """Transport send() routes packets via UDP when addresses are known."""
    alice = create_client("alice")
    bob = create_client("bob")

    # Set up identities
    alice_result = user.new_network(name='Alice', t_ms=1000, db=alice.db)
    alice.peer_id = alice_result['peer_id']
    alice.peer_shared_id = alice_result['peer_shared_id']
    alice.db.commit()

    bob_result = user.new_network(name='Bob', t_ms=1000, db=bob.db)
    bob.peer_id = bob_result['peer_id']
    bob.peer_shared_id = bob_result['peer_shared_id']
    bob.db.commit()

    # Register and enable transport
    udp_transport.register_client(alice)
    udp_transport.register_client(bob)
    udp_transport.enable()

    # Use transport.send() directly with addresses
    test_blob = b"test packet via transport"
    from_addr = (alice.network.host, alice.network.port)
    to_addr = (bob.network.host, bob.network.port)
    transport.send(test_blob, from_addr, to_addr)

    # Route outgoing packets via UDP
    udp_transport.route_outgoing()

    # Give UDP time to deliver
    time.sleep(0.05)

    # Bob should have received it via UDP
    packets = bob.network.drain()
    assert len(packets) == 1
    assert packets[0][0] == test_blob


def test_injected_packets_processed_by_tick(create_client):
    """Packets in transport incoming queue are processed by tick's ReceiveJob."""
    alice = create_client("alice")

    # Set up identity
    alice_result = user.new_network(name='Alice', t_ms=1000, db=alice.db)
    alice.peer_id = alice_result['peer_id']
    alice.peer_shared_id = alice_result['peer_shared_id']
    alice.db.commit()

    # Add a dummy packet directly to the transport incoming queue
    dummy_blob = b"some transit wrapped blob"
    transport.deliver(dummy_blob, ('127.0.0.1', 9999))

    # Verify packet is in incoming queue
    incoming_count, _ = transport.pending_count()
    assert incoming_count == 1

    # Tick will try to process it (will fail unwrap since it's not a real transit blob)
    # But the point is that the packet was drained from the queue
    alice.tick(t_ms=2000)

    # The queue should be empty now (packet was drained)
    incoming_count, _ = transport.pending_count()
    assert incoming_count == 0


def test_udp_packet_injection_and_processing(create_client, udp_transport):
    """UDP packets are properly injected and processed through the tick cycle.

    This tests the full flow:
    1. Client A sends packet via UDP
    2. Client B receives it and injects into transport queue
    3. Client B's tick drains and processes the packet
    """
    # Create two clients with networks
    alice = create_client("alice")
    bob = create_client("bob")

    # Set up identities
    alice_result = user.new_network(name='Alice', t_ms=1000, db=alice.db)
    alice.peer_id = alice_result['peer_id']
    alice.peer_shared_id = alice_result['peer_shared_id']
    alice.db.commit()

    bob_result = user.new_network(name='Bob', t_ms=1000, db=bob.db)
    bob.peer_id = bob_result['peer_id']
    bob.peer_shared_id = bob_result['peer_shared_id']
    bob.db.commit()

    # Register clients with transport
    udp_transport.register_client(alice)
    udp_transport.register_client(bob)
    udp_transport.enable()

    # Send a test packet via transport.send() with addresses
    test_blob = b"test transport integration packet"
    from_addr = (alice.network.host, alice.network.port)
    to_addr = (bob.network.host, bob.network.port)
    transport.send(test_blob, from_addr, to_addr)

    # Route outgoing packets via UDP
    udp_transport.route_outgoing()

    # Give UDP time to deliver
    time.sleep(0.05)

    # Bob's receive_udp_packets will inject it into transport queue
    injected = bob.receive_udp_packets(t_ms=2000)
    assert injected == 1

    # Now verify it's in transport incoming queue
    incoming_count, _ = transport.pending_count()
    assert incoming_count >= 1

    # Bob's tick will process it (drain from transport queue)
    bob.tick(t_ms=2100)

    # Packet should have been drained (processed or discarded)
    incoming_count, _ = transport.pending_count()
    assert incoming_count == 0
    # It won't create events because it's not a valid transit blob,
    # but the infrastructure is working
