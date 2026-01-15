"""
Scenario tests for message emoji reactions.

Based on the three-player messaging pattern, these tests verify that:
- Reactions sync correctly between peers
- Multiple reactions on the same message work correctly
- Reactions converge to identical state across peers
- Removing reactions works
- Message deletion cascade-deletes all reactions
"""
import sqlite3
from core.db import Database
from core import schema
from events.identity import user, invite, peer
from events.content import message, message_deletion, message_reaction
from tests.utils import tick_helper


def test_message_reactions_basic_two_peer(fresh_db):
    """Test basic message reactions with two peers: Alice and Bob."""

    db = fresh_db

    print("\n=== Setup: Create networks and users ===")

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    print(f"Alice created network")

    # Alice creates an invite for Bob
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )
    print(f"Alice created invite")

    # Bob joins Alice's network
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    print(f"Bob joined network")

    db.commit()

    # Initial sync
    print("\n=== Initial sync ===")
    final_t_ms, rounds_used, converged, status = tick_helper.sync_until_converged(
        db=db, start_t_ms=4000, max_rounds=200, check_interval=1, verbose=False
    )
    print(f"Initial sync completed in {rounds_used} rounds")

    # Get channels
    print("\n=== Discovering channels ===")
    alice_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    bob_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (bob['peer_id'],)
    )

    assert len(alice_channels) == 1, f"Alice should have 1 channel, got {len(alice_channels)}"
    assert len(bob_channels) == 1, f"Bob should have 1 channel, got {len(bob_channels)}"

    channel_id = alice_channels[0]['channel_id']
    print(f"Channel ID: {channel_id[:20]}...")

    # Create message
    print("\n=== Alice creates message ===")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Test message for reactions",
        t_ms=5000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()
    print(f"Message created: {message_id[:20]}...")

    # Sync message to Bob
    print("\n=== Sync message to Bob ===")
    final_t_ms, rounds_used, converged, status = tick_helper.sync_until_converged(
        db=db, start_t_ms=6000, max_rounds=200, check_interval=1, verbose=False
    )
    print(f"Message sync completed in {rounds_used} rounds")

    # Verify both see the message
    print("\n=== Verify message visibility ===")
    alice_messages = message.list(channel_id, alice['peer_id'], db)
    bob_messages = message.list(channel_id, bob['peer_id'], db)

    print(f"Alice sees {len(alice_messages)} messages")
    print(f"Bob sees {len(bob_messages)} messages")

    assert len(alice_messages) > 0, "Alice should see the message"
    assert len(bob_messages) > 0, "Bob should see the message"
    assert alice_messages[0]['content'] == "Test message for reactions"
    assert bob_messages[0]['content'] == "Test message for reactions"

    print("\n✅ Base two-peer test passed!")


def test_message_reactions_single_emoji(fresh_db):
    """Test: Alice adds one emoji reaction to a message."""

    db = fresh_db

    print("\n=== Setup ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    print("\n=== Initial sync ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=4000, max_rounds=200, check_interval=1, verbose=False)

    # Get channel and create message
    alice_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    channel_id = alice_channels[0]['channel_id']

    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="React to me!",
        t_ms=5000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    print("\n=== Sync message ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=6000, max_rounds=200, check_interval=1, verbose=False)

    # Alice adds reaction
    print("\n=== Alice adds thumbs up reaction ===")
    reaction_id = message_reaction.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        emoji='👍',
        t_ms=7000,
        db=db
    )
    db.commit()
    print(f"Reaction created: {reaction_id[:20]}...")

    # Check Alice's view before sync
    print("\n=== Check Alice's view (before Bob sync) ===")
    alice_msgs = message.list(channel_id, alice['peer_id'], db)
    reactions = alice_msgs[0].get('reactions', [])
    print(f"Alice sees {len(reactions)} reactions")
    assert len(reactions) == 1, "Alice should see 1 reaction"
    assert reactions[0]['emoji'] == '👍', "Should be thumbs up"
    assert reactions[0]['count'] == 1, "Count should be 1"
    assert 'Alice' in reactions[0]['reactors'], "Alice should be listed as reactor"

    # Sync reaction to Bob
    print("\n=== Sync reaction to Bob ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=8000, max_rounds=200, check_interval=1, verbose=False)

    # Check Bob's view
    print("\n=== Check Bob's view (after sync) ===")
    bob_msgs = message.list(channel_id, bob['peer_id'], db)
    bob_reactions = bob_msgs[0].get('reactions', [])
    print(f"Bob sees {len(bob_reactions)} reactions")
    assert len(bob_reactions) == 1, "Bob should see 1 reaction"
    assert bob_reactions[0]['emoji'] == '👍', "Should be thumbs up"
    assert bob_reactions[0]['count'] == 1, "Count should be 1"
    assert 'Alice' in bob_reactions[0]['reactors'], "Alice should be listed as reactor"

    print("\n✅ Single emoji reaction test passed!")


def test_message_reactions_multiple_emoji(fresh_db):
    """Test: Alice and Bob add different emoji reactions to same message."""

    db = fresh_db

    print("\n=== Setup ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    print("\n=== Initial sync ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=4000, max_rounds=200, check_interval=1, verbose=False)

    # Create message
    alice_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    channel_id = alice_channels[0]['channel_id']

    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="React to me!",
        t_ms=5000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    print("\n=== Sync message ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=6000, max_rounds=200, check_interval=1, verbose=False)

    # Alice adds thumbs up
    print("\n=== Alice adds thumbs up ===")
    alice_reaction = message_reaction.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        emoji='👍',
        t_ms=7000,
        db=db
    )
    db.commit()

    # Bob adds heart
    print("\n=== Bob adds heart ===")
    bob_reaction = message_reaction.create(
        peer_id=bob['peer_id'],
        message_id=message_id,
        emoji='❤️',
        t_ms=7100,
        db=db
    )
    db.commit()

    # Check Alice's view before sync
    print("\n=== Check Alice's view (before sync) ===")
    alice_msgs = message.list(channel_id, alice['peer_id'], db)
    reactions = alice_msgs[0].get('reactions', [])
    print(f"Alice sees {len(reactions)} reactions (local only)")
    assert len(reactions) == 1, "Alice should see 1 reaction locally (her own)"

    # Sync reactions
    print("\n=== Sync reactions ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=8000, max_rounds=200, check_interval=1, verbose=False)

    # Check Alice's view after sync
    print("\n=== Check Alice's view (after sync) ===")
    alice_msgs = message.list(channel_id, alice['peer_id'], db)
    reactions = alice_msgs[0].get('reactions', [])
    print(f"Alice sees {len(reactions)} reactions (after sync)")
    assert len(reactions) == 2, f"Alice should see 2 reactions, got {len(reactions)}"

    # Verify the emojis
    emojis = sorted([r['emoji'] for r in reactions])
    assert '👍' in emojis, "Should have thumbs up"
    assert '❤️' in emojis, "Should have heart"

    # Verify reactor names
    thumbs_up = next(r for r in reactions if r['emoji'] == '👍')
    heart = next(r for r in reactions if r['emoji'] == '❤️')
    assert 'Alice' in thumbs_up['reactors'], "Alice should be thumbs up reactor"
    assert 'Bob' in heart['reactors'], "Bob should be heart reactor"

    # Check Bob's view
    print("\n=== Check Bob's view (after sync) ===")
    bob_msgs = message.list(channel_id, bob['peer_id'], db)
    bob_reactions = bob_msgs[0].get('reactions', [])
    print(f"Bob sees {len(bob_reactions)} reactions")
    assert len(bob_reactions) == 2, "Bob should see 2 reactions"

    # Verify convergence
    alice_reactions_sorted = sorted([r['emoji'] for r in reactions])
    bob_reactions_sorted = sorted([r['emoji'] for r in bob_reactions])
    assert alice_reactions_sorted == bob_reactions_sorted, "Reactions should converge"

    print("\n✅ Multiple emoji reaction test passed!")


def test_message_reactions_removal(fresh_db):
    """Test: Alice removes her reaction and peers converge."""

    db = fresh_db

    print("\n=== Setup ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    tick_helper.sync_until_converged(db=db, start_t_ms=4000, max_rounds=200, check_interval=1, verbose=False)

    # Create message and add reactions
    alice_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    channel_id = alice_channels[0]['channel_id']

    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="React to me!",
        t_ms=5000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    tick_helper.sync_until_converged(db=db, start_t_ms=6000, max_rounds=200, check_interval=1, verbose=False)

    # Both add reactions
    print("\n=== Both peers add reactions ===")
    alice_reaction = message_reaction.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        emoji='👍',
        t_ms=7000,
        db=db
    )
    bob_reaction = message_reaction.create(
        peer_id=bob['peer_id'],
        message_id=message_id,
        emoji='❤️',
        t_ms=7100,
        db=db
    )
    db.commit()

    tick_helper.sync_until_converged(db=db, start_t_ms=8000, max_rounds=200, check_interval=1, verbose=False)

    # Verify both have 2 reactions
    alice_msgs = message.list(channel_id, alice['peer_id'], db)
    reactions = alice_msgs[0].get('reactions', [])
    assert len(reactions) == 2, f"Should have 2 reactions, got {len(reactions)}"
    print(f"✓ Both peers see 2 reactions before removal")

    # Alice removes her thumbs up reaction
    print("\n=== Alice removes thumbs up ===")
    deletion_id = message_reaction.remove(
        peer_id=alice['peer_id'],
        message_id=message_id,
        emoji='👍',
        t_ms=9000,
        db=db
    )
    db.commit()
    print(f"Removal created: {deletion_id[:20]}...")

    # Check Alice's view (should only have heart)
    alice_msgs = message.list(channel_id, alice['peer_id'], db)
    reactions = alice_msgs[0].get('reactions', [])
    print(f"Alice sees {len(reactions)} reactions (before Bob sync)")
    assert len(reactions) == 1, f"Alice should see 1 reaction after removal, got {len(reactions)}"
    assert reactions[0]['emoji'] == '❤️', "Should only have heart"

    # Sync removal to Bob
    print("\n=== Sync removal to Bob ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=10000, max_rounds=200, check_interval=1, verbose=False)

    # Check Bob's view (should only have heart)
    bob_msgs = message.list(channel_id, bob['peer_id'], db)
    bob_reactions = bob_msgs[0].get('reactions', [])
    print(f"Bob sees {len(bob_reactions)} reactions (after sync)")
    assert len(bob_reactions) == 1, f"Bob should see 1 reaction after sync, got {len(bob_reactions)}"
    assert bob_reactions[0]['emoji'] == '❤️', "Should only have heart"

    print("\n✅ Reaction removal test passed!")


def test_message_reactions_cascade_deletion(fresh_db):
    """Test: Deleting message cascade-deletes all reactions."""

    db = fresh_db

    print("\n=== Setup ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    tick_helper.sync_until_converged(db=db, start_t_ms=4000, max_rounds=200, check_interval=1, verbose=False)

    # Create message and add reactions
    alice_channels = db.query_all(
        "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    channel_id = alice_channels[0]['channel_id']

    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Delete me!",
        t_ms=5000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    tick_helper.sync_until_converged(db=db, start_t_ms=6000, max_rounds=200, check_interval=1, verbose=False)

    # Both add reactions
    print("\n=== Both add reactions ===")
    message_reaction.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        emoji='👍',
        t_ms=7000,
        db=db
    )
    message_reaction.create(
        peer_id=bob['peer_id'],
        message_id=message_id,
        emoji='❤️',
        t_ms=7100,
        db=db
    )
    db.commit()

    tick_helper.sync_until_converged(db=db, start_t_ms=8000, max_rounds=200, check_interval=1, verbose=False)

    # Verify both reactions exist
    alice_msgs = message.list(channel_id, alice['peer_id'], db)
    reactions = alice_msgs[0].get('reactions', [])
    assert len(reactions) == 2, "Should have 2 reactions before deletion"
    print(f"✓ Both reactions exist before message deletion")

    # Alice deletes message
    print("\n=== Alice deletes message ===")
    deletion_id = message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=9000,
        db=db
    )
    db.commit()
    print(f"Deletion created: {deletion_id[:20]}...")

    # Check Alice's view (message and reactions should be gone)
    alice_msgs = message.list(channel_id, alice['peer_id'], db)
    print(f"Alice sees {len(alice_msgs)} messages (after deletion)")
    assert len(alice_msgs) == 0, "Alice should see no messages after deletion"

    # Sync deletion to Bob
    print("\n=== Sync deletion to Bob ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=10000, max_rounds=200, check_interval=1, verbose=False)

    # Check Bob's view (message and reactions should be gone)
    bob_msgs = message.list(channel_id, bob['peer_id'], db)
    print(f"Bob sees {len(bob_msgs)} messages (after sync)")
    assert len(bob_msgs) == 0, "Bob should see no messages after deletion sync"

    print("\n✅ Cascade deletion test passed!")
