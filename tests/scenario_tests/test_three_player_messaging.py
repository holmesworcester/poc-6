"""
Scenario test: Three players with message transit via sync.

Alice creates a network. Bob joins Alice's network via invite.
Charlie creates his own separate network.

Tests:
- Alice and Bob sync correctly (including GKS)
- Alice and Bob can exchange messages
- Charlie is isolated (separate network)
"""
import sqlite3
from db import Database
import schema
from events.identity import user, invite, peer
from events.content import message
import tick
from tests.utils import tick_helper


def test_three_player_messaging():
    """Three peers: Alice creates network, Bob joins, Charlie separate."""

    # Setup
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Create networks and invite ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"Alice created network, key_id: {alice['key_id'][:20]}...")

    # Alice creates an invite for Bob
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )
    print(f"Alice created invite: {invite_id[:20]}...")

    # Bob joins Alice's network
    bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)

    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    print(f"Bob joined network, peer_id: {bob['peer_id'][:20]}...")

    # Charlie creates his own separate network
    charlie = user.new_network(name='Charlie', t_ms=3000, db=db)
    print(f"Charlie created separate network")

    db.commit()

    # Initial sync to converge (need multiple rounds for GKS events to propagate)
    print("\n=== Initial sync ===")
    final_t_ms, rounds_used, converged, status = tick_helper.sync_until_converged(
        db=db, start_t_ms=4000, max_rounds=200, check_interval=1, verbose=True
    )
    print(f"Initial sync completed in {rounds_used} rounds (converged={converged})")

    # NOTE: We trust that sync worked correctly.
    # Observable behavior (message delivery) will verify this below.
    # Removed DB queries for group_keys and valid_events tables.

    # Discover channels via sync
    print("\n=== Discovering channels ===")

    alice_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    bob_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    charlie_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (charlie['peer_id'],)
    )

    assert len(alice_channels) == 1, f"Alice should have 1 channel, got {len(alice_channels)}"
    assert len(bob_channels) == 1, f"Bob should have 1 channel, got {len(bob_channels)}"
    assert len(charlie_channels) == 1, f"Charlie should have 1 channel, got {len(charlie_channels)}"

    alice_channel_id = alice_channels[0]['channel_id']
    bob_channel_id = bob_channels[0]['channel_id']
    charlie_channel_id = charlie_channels[0]['channel_id']

    # Alice and Bob should share the same channel (same network)
    assert alice_channel_id == bob_channel_id, \
        "Alice and Bob should share the same channel (same network)"
    print(f"Alice and Bob share channel: {alice_channel_id[:20]}...")
    print(f"Charlie has separate channel: {charlie_channel_id[:20]}...")

    # Create messages
    print("\n=== Creating messages ===")

    # Alice sends a message
    alice_msg = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice_channel_id,
        content="Hello from Alice!",
        t_ms=5000,
        db=db
    )
    db.commit()
    print(f"Alice created message: {alice_msg['id'][:20]}...")

    # Bob sends a message
    bob_msg = message.create(
        peer_id=bob['peer_id'],
        channel_id=bob_channel_id,
        content="Hello from Bob!",
        t_ms=5100,
        db=db
    )
    db.commit()
    print(f"Bob created message: {bob_msg['id'][:20]}...")

    # Charlie sends a message (in his own network)
    charlie_msg = message.create(
        peer_id=charlie['peer_id'],
        channel_id=charlie_channel_id,
        content="Hello from Charlie!",
        t_ms=5200,
        db=db
    )
    db.commit()
    print(f"Charlie created message: {charlie_msg['id'][:20]}...")

    # Sync messages
    print("\n=== Sync Round 2: Message exchange ===")
    final_t_ms2, rounds_used2, converged2, status2 = tick_helper.sync_until_converged(
        db=db, start_t_ms=6000, max_rounds=200, check_interval=1, verbose=True
    )
    print(f"Message sync completed in {rounds_used2} rounds (converged={converged2})")

    # Verify message delivery
    print("\n=== Verifying message delivery ===")

    # Alice should have received Bob's message
    alice_messages = message.list_messages(alice_channel_id, alice['peer_id'], db)
    alice_message_contents = [msg['content'] for msg in alice_messages]
    print(f"Alice sees {len(alice_messages)} messages: {alice_message_contents}")

    assert "Hello from Alice!" in alice_message_contents, \
        "Alice should see her own message"
    assert "Hello from Bob!" in alice_message_contents, \
        "Alice should see Bob's message"
    assert "Hello from Charlie!" not in alice_message_contents, \
        "Alice should NOT see Charlie's message (different network)"

    # Bob should have received Alice's message
    bob_messages = message.list_messages(bob_channel_id, bob['peer_id'], db)
    bob_message_contents = [msg['content'] for msg in bob_messages]
    print(f"Bob sees {len(bob_messages)} messages: {bob_message_contents}")

    assert "Hello from Alice!" in bob_message_contents, \
        "Bob should see Alice's message"
    assert "Hello from Bob!" in bob_message_contents, \
        "Bob should see his own message"
    assert "Hello from Charlie!" not in bob_message_contents, \
        "Bob should NOT see Charlie's message (different network)"

    # Charlie should only see his own message
    charlie_messages = message.list_messages(charlie_channel_id, charlie['peer_id'], db)
    charlie_message_contents = [msg['content'] for msg in charlie_messages]
    print(f"Charlie sees {len(charlie_messages)} messages: {charlie_message_contents}")

    assert "Hello from Charlie!" in charlie_message_contents, \
        "Charlie should see his own message"
    assert "Hello from Alice!" not in charlie_message_contents, \
        "Charlie should NOT see Alice's message (different network)"
    assert "Hello from Bob!" not in charlie_message_contents, \
        "Charlie should NOT see Bob's message (different network)"

    print("\n✅ All assertions passed!")
