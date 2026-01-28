"""
Real networking tests using multiprocess clients.

Each client runs in a separate process with its own:
- Transport module state
- SQLite database
- UDP socket

They communicate via real UDP on localhost.

NO CHEATING:
- Only the joiner gets the inviter's address (simulating out-of-band sharing)
- The inviter learns the joiner's address from the connection request packet
"""
import pytest
import time
import tempfile
import os
import socket
from tests.networking_tests.multiprocess_client import RemoteClient, tick_all, assert_eventually
from tests.networking_tests.ipc import supports_udp_in_subprocess

pytestmark = pytest.mark.skipif(
    not supports_udp_in_subprocess(),
    reason="UDP sockets are not permitted in subprocesses on this platform",
)


def get_free_port():
    """Get a free port for UDP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def alice(tmp_path):
    """Create Alice client in separate process."""
    client = RemoteClient(
        name="alice",
        db_path=str(tmp_path / "alice.db"),
        udp_port=get_free_port()
    )
    client.start()
    yield client
    client.stop()


@pytest.fixture
def bob(tmp_path):
    """Create Bob client in separate process."""
    client = RemoteClient(
        name="bob",
        db_path=str(tmp_path / "bob.db"),
        udp_port=get_free_port()
    )
    client.start()
    yield client
    client.stop()


@pytest.fixture
def charlie(tmp_path):
    """Create Charlie client in separate process."""
    client = RemoteClient(
        name="charlie",
        db_path=str(tmp_path / "charlie.db"),
        udp_port=get_free_port()
    )
    client.start()
    yield client
    client.stop()


class TestRealNetworking:
    """Tests that verify real UDP networking between separate processes."""

    def test_connection_establishment(self, alice, bob):
        """Alice and Bob establish a connection over real UDP."""
        t_ms = 1000

        # Alice creates network
        alice.new_network(name='Alice', t_ms=t_ms)
        t_ms += 100

        # Alice creates invite
        invite_link = alice.create_invite(t_ms=t_ms)
        t_ms += 100

        # Out-of-band: Bob learns Alice's address
        bob.add_peer_address(alice.peer_shared_id, '127.0.0.1', alice.udp_port)

        # Bob creates peer and joins
        bob.create_peer(t_ms=t_ms)
        t_ms += 100

        bob.join(invite_link=invite_link, name='Bob', t_ms=t_ms)
        t_ms += 100

        # NOTE: Alice learns Bob's address from the connection request packet metadata
        # No need to pre-register it - this is the "no cheating" rule

        # Run ticks to establish connection
        tick_all(alice, bob, t_ms=t_ms, rounds=50)
        t_ms += 5000

        # Verify connections
        alice_conns = alice.get_connections(t_ms=t_ms)
        bob_conns = bob.get_connections(t_ms=t_ms)

        alice_active = [c for c in alice_conns if c['can_send']]
        bob_active = [c for c in bob_conns if c['can_send']]

        assert len(alice_active) >= 1, f"Alice should have active connection, got {alice_conns}"
        assert len(bob_active) >= 1, f"Bob should have active connection, got {bob_conns}"

    def test_event_sync(self, alice, bob):
        """Events sync between Alice and Bob over real UDP."""
        t_ms = 1000

        # Setup network
        alice.new_network(name='Alice', t_ms=t_ms)
        t_ms += 100

        invite_link = alice.create_invite(t_ms=t_ms)
        t_ms += 100

        bob.add_peer_address(alice.peer_shared_id, '127.0.0.1', alice.udp_port)
        bob.create_peer(t_ms=t_ms)
        t_ms += 100

        bob.join(invite_link=invite_link, name='Bob', t_ms=t_ms)
        t_ms += 100

        # First establish connection
        tick_all(alice, bob, t_ms=t_ms, rounds=50)
        t_ms += 5000

        # Debug: check sync status before waiting
        alice_status = alice.debug_sync_status(t_ms)
        bob_status = bob.debug_sync_status(t_ms)
        print(f"\n=== After connection establishment ===")
        print(f"Alice connections: {alice_status['connections']}")
        print(f"Alice shareable events: {alice_status['shareable_events']}")
        print(f"Alice root hash: {alice_status['root_hash']}")
        print(f"Bob connections: {bob_status['connections']}")
        print(f"Bob shareable events: {bob_status['shareable_events']}")
        print(f"Bob root hash: {bob_status['root_hash']}")

        # Wait for sync to complete - Bob should see at least 2 users
        def check_users_synced():
            try:
                bob_users = bob.get_users()
                return len(bob_users) >= 2
            except Exception:
                return False

        t_ms = assert_eventually(
            check_users_synced,
            alice, bob,
            t_ms=t_ms,
            max_rounds=200,
            msg="Bob should see at least 2 users after sync"
        )

        # Verify channels synced too
        bob_channels = bob.get_channels()
        assert len(bob_channels) >= 1, f"Bob should have at least 1 channel, got {bob_channels}"

    def test_message_sync(self, alice, bob):
        """Alice sends a message, Bob receives it over real UDP."""
        t_ms = 1000

        # Setup
        alice.new_network(name='Alice', t_ms=t_ms)
        t_ms += 100

        invite_link = alice.create_invite(t_ms=t_ms)
        t_ms += 100

        bob.add_peer_address(alice.peer_shared_id, '127.0.0.1', alice.udp_port)
        bob.create_peer(t_ms=t_ms)
        t_ms += 100

        bob.join(invite_link=invite_link, name='Bob', t_ms=t_ms)
        t_ms += 100


        # Sync to establish connection
        tick_all(alice, bob, t_ms=t_ms, rounds=50)
        t_ms += 5000

        # Alice sends a message
        msg_id = alice.send_message(content="Hello Bob over UDP!", t_ms=t_ms)
        t_ms += 100

        # Sync the message
        tick_all(alice, bob, t_ms=t_ms, rounds=50)
        t_ms += 5000

        # Bob should have received it (use Alice's channel_id since Bob synced it)
        bob_messages = bob.get_messages(channel_id=alice.channel_id)

        assert len(bob_messages) >= 1, f"Bob should have at least 1 message, got {bob_messages}"

        # Find the specific message
        found = any(m.get('content') == "Hello Bob over UDP!" for m in bob_messages)
        assert found, f"Bob should have Alice's message, got {bob_messages}"

    def test_bidirectional_messaging(self, alice, bob):
        """Alice and Bob both send messages to each other."""
        t_ms = 1000

        # Setup
        alice.new_network(name='Alice', t_ms=t_ms)
        t_ms += 100

        invite_link = alice.create_invite(t_ms=t_ms)
        t_ms += 100

        bob.add_peer_address(alice.peer_shared_id, '127.0.0.1', alice.udp_port)
        bob.create_peer(t_ms=t_ms)
        t_ms += 100

        bob.join(invite_link=invite_link, name='Bob', t_ms=t_ms)
        t_ms += 100


        # Sync to establish connection and sync channel to Bob
        tick_all(alice, bob, t_ms=t_ms, rounds=50)
        t_ms += 5000

        # Alice sends message
        alice_msg_id = alice.send_message(content="Hello from Alice!", t_ms=t_ms)
        t_ms += 100

        # Sync
        tick_all(alice, bob, t_ms=t_ms, rounds=30)
        t_ms += 3000

        # Bob sends message (using Alice's channel which he synced)
        bob_msg_id = bob.send_message(content="Hello from Bob!", t_ms=t_ms, channel_id=alice.channel_id)
        t_ms += 100

        # Sync more
        tick_all(alice, bob, t_ms=t_ms, rounds=30)
        t_ms += 3000

        # Verify both received each other's messages
        alice_messages = alice.get_messages(channel_id=alice.channel_id)
        bob_messages = bob.get_messages(channel_id=alice.channel_id)

        alice_has_bob = any(m.get('content') == "Hello from Bob!" for m in alice_messages)
        bob_has_alice = any(m.get('content') == "Hello from Alice!" for m in bob_messages)

        assert bob_has_alice, f"Bob should have Alice's message, got {bob_messages}"
        assert alice_has_bob, f"Alice should have Bob's message, got {alice_messages}"


class TestThreePlayerNetworking:
    """Tests with three participants."""

    def test_three_player_sync(self, alice, bob, charlie):
        """Three players can all sync with each other."""
        t_ms = 1000

        # Alice creates network
        alice.new_network(name='Alice', t_ms=t_ms)
        t_ms += 100

        # Alice invites Bob
        invite_bob = alice.create_invite(t_ms=t_ms)
        t_ms += 100

        bob.add_peer_address(alice.peer_shared_id, '127.0.0.1', alice.udp_port)
        bob.create_peer(t_ms=t_ms)
        t_ms += 100

        bob.join(invite_link=invite_bob, name='Bob', t_ms=t_ms)
        t_ms += 100


        # Sync Alice and Bob
        tick_all(alice, bob, t_ms=t_ms, rounds=50)
        t_ms += 5000

        # Alice invites Charlie (only admins can create invites)
        invite_charlie = alice.create_invite(t_ms=t_ms)
        t_ms += 100

        charlie.add_peer_address(alice.peer_shared_id, '127.0.0.1', alice.udp_port)
        charlie.create_peer(t_ms=t_ms)
        t_ms += 100

        charlie.join(invite_link=invite_charlie, name='Charlie', t_ms=t_ms)
        t_ms += 100


        # Sync all three
        tick_all(alice, bob, charlie, t_ms=t_ms, rounds=100)
        t_ms += 10000

        # All should see all users
        alice_users = alice.get_users()
        bob_users = bob.get_users()
        charlie_users = charlie.get_users()

        assert len(alice_users) >= 3, f"Alice should see 3 users, got {len(alice_users)}"
        assert len(bob_users) >= 3, f"Bob should see 3 users, got {len(bob_users)}"
        assert len(charlie_users) >= 3, f"Charlie should see 3 users, got {len(charlie_users)}"
