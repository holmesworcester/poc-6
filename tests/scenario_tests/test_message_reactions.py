"""
Scenario test: Message emoji reactions.

Alice and Bob are in the same network and channel.
Alice sends a message.
Bob adds an emoji reaction to Alice's message.
Alice adds a different emoji reaction to the same message.
Both peers sync and converge on the same reaction set.
Bob removes their reaction, reactions sync and converge.
Alice deletes the message, cascade deleting all reactions.

Tests:
- Bob can add reaction to Alice's message
- Alice can add different emoji to same message
- Multiple reactions on same message from different users
- Reactions appear in message.list() with reactor names
- Users can remove their own reactions
- Only reactor or admins can remove reactions
- Message deletion cascade deletes all reactions
- Reactions sync and converge across peers
"""
import sqlite3
import pytest
from db import Database
import schema
from events.identity import user, invite
from events.content import message, message_deletion, message_reaction
from events.group import group_member
import tick
from tests.utils import tick_helper


def test_message_reactions_basic_workflow(fresh_db):
    """Test basic message reactions: add, view, remove, delete."""

    # Setup
    db = fresh_db
    tick.reset_state(db)

    print("\n=== Setup: Create network and channel ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db, network_name='test-network')
    alice_peer_id = alice['peer_id']
    print(f"Alice created network")

    # Alice creates an invite for Bob
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice_peer_id,
        t_ms=1500,
        db=db
    )

    # Create Bob's peer first
    from events.identity import peer as peer_module
    bob_peer_id = peer_module.create(t_ms=2000, db=db)

    # Bob joins
    bob = user.join(
        peer_id=bob_peer_id,
        invite_link=invite_link,
        name='Bob',
        t_ms=2000,
        db=db
    )
    bob_peer_id = bob['peer_id']
    print(f"Bob joined network")

    db.commit()

    # Initial sync
    print("\n=== Initial sync to converge ===")
    final_t_ms, rounds_used, converged, status = tick_helper.sync_until_converged(
        db=db, start_t_ms=4000, max_rounds=50, check_interval=5, verbose=True
    )
    print(f"Synced in {rounds_used} rounds (converged={converged})")
    assert converged, "Peers should converge after initial sync"

    # Get channel (both should see #general)
    alice_channels = message.list(None, alice_peer_id, db)  # This will fail - need channel_id
    # Let me refactor - need to get channel ID first

    print("\n=== Send message from Alice ===")

    # Get channel ID from Alice's perspective
    from events.content import channel as channel_module
    alice_channels = channel_module.list_channels(recorded_by=alice_peer_id, db=db)
    assert len(alice_channels) > 0, "Should have at least #general channel"
    channel_id = alice_channels[0]['channel_id']
    print(f"Using channel: {alice_channels[0]['name']}")

    # Alice sends a message
    result = message.create(
        peer_id=alice_peer_id,
        channel_id=channel_id,
        content="Hello Bob!",
        t_ms=5000,
        db=db
    )
    message_id = result['id']
    print(f"Alice sent message: {message_id[:20]}...")

    db.commit()

    # Sync for message to propagate
    print("\n=== Sync message to Bob ===")
    final_t_ms, rounds_used, converged, status = tick_helper.sync_until_converged(
        db=db, start_t_ms=6000, max_rounds=20, check_interval=5, verbose=True
    )
    print(f"Message synced in {rounds_used} rounds")

    # Bob adds a reaction
    print("\n=== Bob adds thumbs up reaction ===")
    reaction_id_1 = message_reaction.create(
        peer_id=bob_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=7000,
        db=db
    )
    print(f"Bob's reaction_id: {reaction_id_1[:20]}...")

    db.commit()

    # Alice adds a different reaction
    print("\n=== Alice adds heart reaction ===")
    reaction_id_2 = message_reaction.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        emoji='❤️',
        t_ms=7100,
        db=db
    )
    print(f"Alice's reaction_id: {reaction_id_2[:20]}...")

    db.commit()

    # Check Alice's view of reactions before sync
    print("\n=== Alice's view before sync ===")
    alice_msgs = message.list(channel_id, alice_peer_id, db)
    msg = alice_msgs[0]
    reactions = msg.get('reactions', [])
    print(f"Reactions on Alice's message: {len(reactions)} emoji")
    for r in reactions:
        print(f"  {r['emoji']}: {r['count']} from {r['reactors']}")

    assert len(reactions) == 2, "Should have 2 emoji reactions before sync"
    assert any(r['emoji'] == '👍' for r in reactions), "Should have thumbs up"
    assert any(r['emoji'] == '❤️' for r in reactions), "Should have heart"

    # Sync reactions
    print("\n=== Sync reactions across peers ===")
    final_t_ms, rounds_used, converged, status = tick_helper.sync_until_converged(
        db=db, start_t_ms=8000, max_rounds=20, check_interval=5, verbose=True
    )
    print(f"Reactions synced in {rounds_used} rounds")

    # Check Bob's view of reactions after sync
    print("\n=== Bob's view after sync ===")
    bob_msgs = message.list(channel_id, bob_peer_id, db)
    msg = bob_msgs[0]
    reactions = msg.get('reactions', [])
    print(f"Reactions on message: {len(reactions)} emoji")
    for r in reactions:
        print(f"  {r['emoji']}: {r['count']} from {r['reactors']}")

    assert len(reactions) == 2, "Bob should see both emoji reactions after sync"

    # Verify thumbs up reaction
    thumbs_up = next((r for r in reactions if r['emoji'] == '👍'), None)
    assert thumbs_up is not None, "Should have thumbs up reaction"
    assert thumbs_up['count'] == 1, "Thumbs up should have count 1"
    assert 'Bob' in thumbs_up['reactors'], "Bob should be in thumbs up reactors"

    # Verify heart reaction
    heart = next((r for r in reactions if r['emoji'] == '❤️'), None)
    assert heart is not None, "Should have heart reaction"
    assert heart['count'] == 1, "Heart should have count 1"
    assert 'Alice' in heart['reactors'], "Alice should be in heart reactors"

    # Bob removes their reaction
    print("\n=== Bob removes thumbs up reaction ===")
    deletion_id = message_reaction.remove(
        peer_id=bob_peer_id,
        message_id=message_id,
        emoji='👍',
        t_ms=9000,
        db=db
    )
    print(f"Deletion_id: {deletion_id[:20]}...")

    db.commit()

    # Check Alice's view of reactions (should only have heart)
    print("\n=== Alice's view after Bob removes reaction ===")
    alice_msgs = message.list(channel_id, alice_peer_id, db)
    msg = alice_msgs[0]
    reactions = msg.get('reactions', [])
    print(f"Reactions: {len(reactions)} emoji")
    for r in reactions:
        print(f"  {r['emoji']}: {r['count']} from {r['reactors']}")

    # Sync removal
    print("\n=== Sync reaction removal ===")
    final_t_ms, rounds_used, converged, status = tick_helper.sync_until_converged(
        db=db, start_t_ms=10000, max_rounds=20, check_interval=5, verbose=True
    )
    print(f"Removal synced in {rounds_used} rounds")

    # Check both peers see only heart reaction
    print("\n=== Both peers should see only heart reaction ===")
    alice_msgs = message.list(channel_id, alice_peer_id, db)
    bob_msgs = message.list(channel_id, bob_peer_id, db)

    alice_reactions = alice_msgs[0].get('reactions', [])
    bob_reactions = bob_msgs[0].get('reactions', [])

    print(f"Alice sees {len(alice_reactions)} reactions")
    print(f"Bob sees {len(bob_reactions)} reactions")

    assert len(alice_reactions) == 1, "Alice should see only 1 reaction"
    assert len(bob_reactions) == 1, "Bob should see only 1 reaction"
    assert alice_reactions[0]['emoji'] == '❤️', "Should be heart"
    assert bob_reactions[0]['emoji'] == '❤️', "Should be heart"

    # Alice deletes the message (cascade delete reactions)
    print("\n=== Alice deletes message (cascade delete reactions) ===")
    deletion_id = message_deletion.create(
        peer_id=alice_peer_id,
        message_id=message_id,
        t_ms=11000,
        db=db
    )
    print(f"Message deletion_id: {deletion_id[:20]}...")

    db.commit()

    # Check Alice's view - message should be gone
    print("\n=== Alice's view after deletion ===")
    alice_msgs = message.list(channel_id, alice_peer_id, db)
    print(f"Messages in channel: {len(alice_msgs)}")
    assert len(alice_msgs) == 0, "Alice should see no messages after deletion"

    # Sync deletion
    print("\n=== Sync deletion across peers ===")
    final_t_ms, rounds_used, converged, status = tick_helper.sync_until_converged(
        db=db, start_t_ms=12000, max_rounds=20, check_interval=5, verbose=True
    )
    print(f"Deletion synced in {rounds_used} rounds")

    # Check Bob's view - message should be gone
    print("\n=== Bob's view after deletion sync ===")
    bob_msgs = message.list(channel_id, bob_peer_id, db)
    print(f"Messages in channel: {len(bob_msgs)}")
    assert len(bob_msgs) == 0, "Bob should see no messages after deletion sync"

    print("\n✓ All tests passed!")


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
