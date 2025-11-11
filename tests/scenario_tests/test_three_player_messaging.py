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
    for i in range(5):
        print(f"\n=== Sync Round {i+1} ===")
        tick.tick(t_ms=4000 + i*200, db=db)

    # Check that Bob has Alice's network key (from GKS)
    bob_has_network_key = db.query_one(
        "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (alice['key_id'], bob['peer_id'])
    )
    print(f"Bob has Alice's network key: {bool(bob_has_network_key)}")

    assert bob_has_network_key, \
        f"Bob should have Alice's network key {alice['key_id']} after sync (received via group_key_shared)"

    # Check that channel is valid for Bob
    bob_channel_valid = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob['channel_id'], bob['peer_id'])
    )
    print(f"Bob has valid channel: {bool(bob_channel_valid)}")

    # Create messages
    print("\n=== Creating messages ===")

    # Alice sends a message
    alice_msg = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Hello from Alice!",
        t_ms=5000,
        db=db
    )
    db.commit()
    print(f"Alice created message: {alice_msg['id'][:20]}...")

    # Bob sends a message
    bob_msg = message.create(
        peer_id=bob['peer_id'],
        channel_id=bob['channel_id'],
        content="Hello from Bob!",
        t_ms=5100,
        db=db
    )
    db.commit()
    print(f"Bob created message: {bob_msg['id'][:20]}...")

    # Charlie sends a message (in his own network)
    charlie_msg = message.create(
        peer_id=charlie['peer_id'],
        channel_id=charlie['channel_id'],
        content="Hello from Charlie!",
        t_ms=5200,
        db=db
    )
    db.commit()
    print(f"Charlie created message: {charlie_msg['id'][:20]}...")

    # Sync messages
    print("\n=== Sync Round 2: Message exchange ===")
    for round_num in range(3):  # A few rounds to ensure convergence
        tick.tick(t_ms=6000 + round_num * 100, db=db)

    # Verify message delivery
    print("\n=== Verifying message delivery ===")

    # Alice should have received Bob's message
    alice_messages = message.list_messages(alice['channel_id'], alice['peer_id'], db)
    alice_message_contents = [msg['content'] for msg in alice_messages]
    print(f"Alice sees {len(alice_messages)} messages: {alice_message_contents}")

    assert "Hello from Alice!" in alice_message_contents, \
        "Alice should see her own message"
    assert "Hello from Bob!" in alice_message_contents, \
        "Alice should see Bob's message"
    assert "Hello from Charlie!" not in alice_message_contents, \
        "Alice should NOT see Charlie's message (different network)"

    # Bob should have received Alice's message
    bob_messages = message.list_messages(bob['channel_id'], bob['peer_id'], db)
    bob_message_contents = [msg['content'] for msg in bob_messages]
    print(f"Bob sees {len(bob_messages)} messages: {bob_message_contents}")

    assert "Hello from Alice!" in bob_message_contents, \
        "Bob should see Alice's message"
    assert "Hello from Bob!" in bob_message_contents, \
        "Bob should see his own message"
    assert "Hello from Charlie!" not in bob_message_contents, \
        "Bob should NOT see Charlie's message (different network)"

    # Charlie should only see his own message
    charlie_messages = message.list_messages(charlie['channel_id'], charlie['peer_id'], db)
    charlie_message_contents = [msg['content'] for msg in charlie_messages]
    print(f"Charlie sees {len(charlie_messages)} messages: {charlie_message_contents}")

    assert "Hello from Charlie!" in charlie_message_contents, \
        "Charlie should see his own message"
    assert "Hello from Alice!" not in charlie_message_contents, \
        "Charlie should NOT see Alice's message (different network)"
    assert "Hello from Bob!" not in charlie_message_contents, \
        "Charlie should NOT see Bob's message (different network)"

    print("\n✅ All assertions passed!")
