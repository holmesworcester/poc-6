"""
Tests for sync over real UDP transport.

These tests verify that events can be synced between separate client databases
via actual UDP packets on localhost.
"""
import time
from events.identity import user
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
    """Transport callback intercepts outgoing packets."""
    from core import queues

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

    # Use queues.incoming.add directly with to_peer=bob's peer_shared_id
    from core.db import create_unsafe_db
    unsafedb = create_unsafe_db(alice.db)

    test_blob = b"test packet via callback"
    result = queues.incoming.add(
        test_blob,
        t_ms=2000,
        unsafedb=unsafedb,
        from_peer=alice.peer_id,
        to_peer=bob.peer_shared_id
    )

    assert result is True  # Packet was handled

    # Give UDP time to deliver
    time.sleep(0.05)

    # Bob should have received it via UDP
    packets = bob.network.drain()
    assert len(packets) == 1
    assert packets[0][0] == test_blob


def test_injected_packets_processed_by_tick(create_client):
    """Packets in SQLite queue are processed by tick's sync_receive."""
    from core import queues
    from core.db import create_unsafe_db

    alice = create_client("alice")

    # Set up identity
    alice_result = user.new_network(name='Alice', t_ms=1000, db=alice.db)
    alice.peer_id = alice_result['peer_id']
    alice.peer_shared_id = alice_result['peer_shared_id']
    alice.db.commit()

    # Add a dummy packet directly to the SQLite queue (simulating real network arrival)
    unsafedb = create_unsafe_db(alice.db)
    dummy_blob = b"some transit wrapped blob"
    queues.incoming.add_immediate(dummy_blob, t_ms=1500, unsafedb=unsafedb)
    alice.db.commit()

    # Verify packet is in SQLite queue
    pending = queues.incoming.pending_count(current_time_ms=2000, unsafedb=unsafedb)
    assert pending == 1

    # Tick will try to process it (will fail unwrap since it's not a real transit blob)
    # But the point is that the packet was drained from the queue
    alice.tick(t_ms=2000)

    # The queue should be empty now (packet was drained)
    pending = queues.incoming.pending_count(current_time_ms=2000, unsafedb=unsafedb)
    assert pending == 0


def test_udp_packet_injection_and_processing(create_client, udp_transport):
    """UDP packets are properly injected and processed through the tick cycle.

    This tests the full flow:
    1. Client A sends packet via UDP
    2. Client B receives it and injects into SQLite queue
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

    # Send a test packet via the transport callback
    # This simulates what connection.send() does
    from core import queues
    from core.db import create_unsafe_db

    test_blob = b"test transport integration packet"
    alice_unsafedb = create_unsafe_db(alice.db)

    # Use queues.incoming.add which will route through transport callback
    result = queues.incoming.add(
        test_blob,
        t_ms=2000,
        unsafedb=alice_unsafedb,
        from_peer=alice.peer_id,
        to_peer=bob.peer_shared_id
    )
    assert result is True

    # Give UDP time to deliver
    time.sleep(0.05)

    # Bob's UDP socket should have the packet
    # receive_udp_packets will inject it into Bob's SQLite queue
    injected = bob.receive_udp_packets(t_ms=2000)
    assert injected == 1

    # Now verify it's in Bob's SQLite queue
    bob_unsafedb = create_unsafe_db(bob.db)
    pending = queues.incoming.pending_count(current_time_ms=2100, unsafedb=bob_unsafedb)
    assert pending >= 1

    # Bob's tick will process it (drain from SQLite queue)
    bob.tick(t_ms=2100)

    # Packet should have been drained (processed or discarded)
    pending = queues.incoming.pending_count(current_time_ms=2100, unsafedb=bob_unsafedb)
    assert pending == 0
    # It won't create events because it's not a valid transit blob,
    # but the infrastructure is working
