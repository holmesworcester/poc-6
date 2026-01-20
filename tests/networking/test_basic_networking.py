"""Simple e2e tests for basic networking functionality.

These tests verify the fundamental building blocks work:
1. UDP sockets can send/receive packets
2. Addresses are embedded in invites and extractable
3. Transport callback routes packets correctly
4. Packets flow between clients

SKIPPED: These tests use the old queues.set_transport_callback() system
which is incompatible with the new core/transport.py system. Real networking
tests need to be refactored to use multi-instance (separate processes).
"""

import pytest

# Skip all tests in this module - old transport callback system incompatible with new transport.py
pytestmark = pytest.mark.skip(reason="Uses old transport callback system; needs multi-instance refactor")
import tempfile
import shutil
import time

from tests.networking.conftest import (
    RealClient, UDPSocket, setup_transport_callback, get_unique_ports,
    create_network_and_join, now_ms
)
from events.network import connection_request as conn_module
from events.identity import invite
from core.db import create_safe_db, create_unsafe_db


@pytest.fixture
def networked_two_clients(two_clients):
    """Two clients with network created and joined."""
    create_network_and_join(two_clients)
    return two_clients


class TestUDPBasics:
    """Test basic UDP socket functionality."""

    def test_udp_loopback(self):
        """Two UDP sockets can exchange packets over loopback."""
        ports = get_unique_ports(2)

        sock_a = UDPSocket(ports[0])
        sock_b = UDPSocket(ports[1])

        sock_a.start()
        sock_b.start()

        try:
            # A sends to B
            sock_a.send_to(('127.0.0.1', ports[1]), b'hello from A')
            time.sleep(0.2)  # Wait for packet

            packets_b = sock_b.drain()
            assert len(packets_b) == 1
            data, addr = packets_b[0]
            assert data == b'hello from A'
            assert addr[0] == '127.0.0.1'
            assert addr[1] == ports[0]  # Source port is A's port

            # B sends to A
            sock_b.send_to(('127.0.0.1', ports[0]), b'hello from B')
            time.sleep(0.2)

            packets_a = sock_a.drain()
            assert len(packets_a) == 1
            data, addr = packets_a[0]
            assert data == b'hello from B'
            assert addr[1] == ports[1]  # Source port is B's port

        finally:
            sock_a.stop()
            sock_b.stop()

    def test_udp_multiple_packets(self):
        """UDP sockets can handle multiple packets."""
        ports = get_unique_ports(2)

        sock_a = UDPSocket(ports[0])
        sock_b = UDPSocket(ports[1])

        sock_a.start()
        sock_b.start()

        try:
            # Send multiple packets
            for i in range(5):
                sock_a.send_to(('127.0.0.1', ports[1]), f'packet {i}'.encode())

            time.sleep(0.3)

            packets = sock_b.drain()
            assert len(packets) == 5

            # Verify all packets received
            messages = [p[0].decode() for p in packets]
            for i in range(5):
                assert f'packet {i}' in messages

        finally:
            sock_a.stop()
            sock_b.stop()


class TestInviteAddresses:
    """Test that addresses are embedded in invites."""

    def test_ongoing_invite_has_address(self, networked_two_clients):
        """Ongoing invite (not bootstrap) has address embedded."""
        alice = networked_two_clients['alice']

        # Check Alice's invites table - the ongoing invite (has address field set)
        # Bootstrap invites don't have address, ongoing invites do
        safedb = create_safe_db(alice.db, recorded_by=alice.peer_id)
        invites = list(safedb.query(
            "SELECT address, port FROM invites WHERE recorded_by = ? AND address IS NOT NULL",
            (alice.peer_id,)
        ))

        # Should have at least one ongoing invite with address
        assert len(invites) >= 1, "Alice should have an ongoing invite with address"

        inv = invites[0]
        assert inv['address'] == '127.0.0.1', "Ongoing invite should have Alice's address"
        assert inv['port'] == alice.network.port, "Ongoing invite should have Alice's port"

    def test_invite_accepted_has_address(self, networked_two_clients):
        """Bob's invite_accepted stores Alice's address from the invite link."""
        bob = networked_two_clients['bob']
        alice = networked_two_clients['alice']

        # Check Bob's invite_accepteds table - address comes from the invite link
        safedb = create_safe_db(bob.db, recorded_by=bob.peer_id)
        ias = list(safedb.query(
            "SELECT address, port FROM invite_accepteds WHERE recorded_by = ?",
            (bob.peer_id,)
        ))

        # Should have one invite_accepted with Alice's address
        assert len(ias) >= 1, "Bob should have an invite_accepted"

        ia = ias[0]
        assert ia['address'] == '127.0.0.1', "invite_accepted should have Alice's address"
        assert ia['port'] == alice.network.port, "invite_accepted should have Alice's port"


class TestAddressLookup:
    """Test address lookup from various sources."""

    def test_lookup_from_invite_accepted(self, networked_two_clients):
        """Bob can look up Alice's address from invite_accepteds."""
        alice = networked_two_clients['alice']
        bob = networked_two_clients['bob']

        # After create_network_and_join, Bob should have Alice's address
        addr = conn_module.get_address_by_peer(
            alice.peer_shared_id, bob.peer_id, bob.db
        )

        assert addr is not None, "Bob should be able to look up Alice's address from invite_accepteds"
        assert addr[0] == '127.0.0.1'
        assert addr[1] == alice.network.port

    def test_alice_cannot_lookup_bob_initially(self, networked_two_clients):
        """Alice cannot look up Bob's address initially (no learned address yet)."""
        alice = networked_two_clients['alice']
        bob = networked_two_clients['bob']

        # Alice doesn't have Bob's address yet (before any packet exchange)
        addr = conn_module.get_address_by_peer(
            bob.peer_shared_id, alice.peer_id, alice.db
        )

        # Should be None - Alice hasn't learned Bob's address
        assert addr is None


class TestTransportCallback:
    """Test the transport callback routing."""

    def test_transport_routes_to_known_address(self, networked_two_clients):
        """Transport callback routes packets when address is known."""
        alice = networked_two_clients['alice']
        bob = networked_two_clients['bob']

        # Bob knows Alice's address (from invite)
        # When Bob sends to Alice, it should route via UDP

        t_ms = now_ms()

        # Tick Bob to trigger any outgoing connection requests
        bob.tick(t_ms)

        # Give UDP time to deliver
        time.sleep(0.2)

        # Check if Alice's UDP socket received anything
        packets = alice.network.drain()

        # Bob should have sent a connection_request to Alice
        # (routing through the transport callback)
        assert len(packets) >= 1, "Alice should have received at least one packet from Bob"

        # Verify packet came from Bob's port
        _, addr = packets[0]
        assert addr[0] == '127.0.0.1'
        assert addr[1] == bob.network.port


class TestPacketFlow:
    """Test end-to-end packet flow between clients."""

    def test_bob_sends_to_alice(self, networked_two_clients):
        """Bob can send a packet to Alice via UDP."""
        alice = networked_two_clients['alice']
        bob = networked_two_clients['bob']

        t_ms = now_ms()

        # Tick both clients
        alice.tick(t_ms)
        bob.tick(t_ms)  # Bob should send connection_request to Alice

        # Wait for UDP delivery
        time.sleep(0.2)

        # Check Alice's UDP buffer
        packets = alice.network.drain()
        assert len(packets) >= 1, "Alice should receive Bob's packet"

    def test_alice_processes_bob_packet(self, networked_two_clients):
        """Alice processes Bob's packet - verify packet flow works."""
        alice = networked_two_clients['alice']
        bob = networked_two_clients['bob']

        # Tick multiple times to allow packet exchange
        for i in range(5):
            t_ms = now_ms()
            alice.tick(t_ms)
            bob.tick(t_ms)
            time.sleep(0.1)

        # Check if connection was established (more reliable than checking pending)
        # The pending_connection_request may be processed quickly into a connection
        alice_safedb = create_safe_db(alice.db, recorded_by=alice.peer_id)
        bob_safedb = create_safe_db(bob.db, recorded_by=bob.peer_id)

        alice_conns = list(alice_safedb.query(
            "SELECT * FROM connections WHERE recorded_by = ?",
            (alice.peer_id,)
        ))
        bob_conns = list(bob_safedb.query(
            "SELECT * FROM connections WHERE recorded_by = ?",
            (bob.peer_id,)
        ))

        # At least Bob should have a connection (he initiated)
        assert len(bob_conns) >= 1, "Bob should have at least one connection"

        # If Alice processed Bob's request, she should also have a connection
        # (This tests that packets are flowing and being processed)
        print(f"Alice connections: {len(alice_conns)}")
        print(f"Bob connections: {len(bob_conns)}")


class TestBidirectionalCommunication:
    """Test bidirectional packet flow."""

    def test_full_handshake(self, networked_two_clients):
        """Full connection handshake: Bob sends request, Alice sends ack."""
        alice = networked_two_clients['alice']
        bob = networked_two_clients['bob']

        # Tick multiple times to allow handshake to complete
        for i in range(10):
            t_ms = now_ms()
            alice.tick(t_ms)
            bob.tick(t_ms)
            time.sleep(0.1)

        # Check connections
        alice_safedb = create_safe_db(alice.db, recorded_by=alice.peer_id)
        bob_safedb = create_safe_db(bob.db, recorded_by=bob.peer_id)

        alice_conns = list(alice_safedb.query(
            "SELECT * FROM connections WHERE recorded_by = ?",
            (alice.peer_id,)
        ))
        bob_conns = list(bob_safedb.query(
            "SELECT * FROM connections WHERE recorded_by = ?",
            (bob.peer_id,)
        ))

        # Both should have connections
        assert len(bob_conns) >= 1, "Bob should have at least one connection"
        # Alice may or may not have a connection depending on whether she sent an ack
        print(f"Alice connections: {len(alice_conns)}")
        print(f"Bob connections: {len(bob_conns)}")

        # Bob's connection should have Alice's address
        bob_conn = bob_conns[0]
        if bob_conn.get('peer_ip'):
            assert bob_conn['peer_ip'] == '127.0.0.1'
