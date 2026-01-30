"""
Scenario tests for message emoji reactions.

Tests verify that:
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
from tests.utils.tick_helper import assert_eventually, TestClock


def test_message_reactions_basic_two_peer(fresh_db):
    """Test basic message reactions with two peers: Alice and Bob."""
    db = fresh_db
    clock = TestClock()

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)

    # Alice creates an invite for Bob
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)

    # Bob joins Alice's network
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    channel_id = alice['channel_id']

    # Wait for Bob to see channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) == 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=None)

    # Create message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Test message for reactions",
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Wait for Bob to see the message
    def bob_sees_message():
        bob_messages = message.list(channel_id, bob['peer_id'], db)
        assert len(bob_messages) > 0
        assert bob_messages[0]['content'] == "Test message for reactions"

    assert_eventually(bob_sees_message, db=db, start_t_ms=t_ms)


def test_message_reactions_single_emoji(fresh_db):
    """Test: Alice adds one emoji reaction to a message."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    channel_id = alice['channel_id']

    # Wait for Bob to see channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) == 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=None)

    # Create message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="React to me!",
        t_ms=t_ms,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Wait for Bob to see the message
    def bob_sees_message():
        bob_msgs = message.list(channel_id, bob['peer_id'], db)
        assert len(bob_msgs) > 0

    t_ms = assert_eventually(bob_sees_message, db=db, start_t_ms=t_ms)

    # Alice adds reaction
    message_reaction.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        emoji='👍',
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Verify Alice sees reaction
    alice_msgs = message.list(channel_id, alice['peer_id'], db)
    reactions = alice_msgs[0].get('reactions', [])
    assert len(reactions) == 1, "Alice should see 1 reaction"
    assert reactions[0]['emoji'] == '👍', "Should be thumbs up"
    assert reactions[0]['count'] == 1, "Count should be 1"
    assert 'Alice' in reactions[0]['reactors'], "Alice should be listed as reactor"

    # Wait for Bob to see the reaction
    def bob_sees_reaction():
        bob_msgs = message.list(channel_id, bob['peer_id'], db)
        bob_reactions = bob_msgs[0].get('reactions', [])
        assert len(bob_reactions) == 1, "Bob should see 1 reaction"
        assert bob_reactions[0]['emoji'] == '👍', "Should be thumbs up"
        assert bob_reactions[0]['count'] == 1, "Count should be 1"
        assert 'Alice' in bob_reactions[0]['reactors'], "Alice should be listed as reactor"

    assert_eventually(bob_sees_reaction, db=db, start_t_ms=t_ms)


def test_message_reactions_multiple_emoji(fresh_db):
    """Test: Alice and Bob add different emoji reactions to same message."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    channel_id = alice['channel_id']

    # Wait for Bob to see channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) == 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=None)

    # Create message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="React to me!",
        t_ms=t_ms,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Wait for Bob to see the message
    def bob_sees_message():
        bob_msgs = message.list(channel_id, bob['peer_id'], db)
        assert len(bob_msgs) > 0

    t_ms = assert_eventually(bob_sees_message, db=db, start_t_ms=t_ms)

    # Alice adds thumbs up
    message_reaction.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        emoji='👍',
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Bob adds heart
    message_reaction.create(
        peer_id=bob['peer_id'],
        message_id=message_id,
        emoji='❤️',
        t_ms=t_ms + 100,
        db=db
    )
    db.commit()

    # Wait for both peers to see both reactions
    def both_see_both_reactions():
        alice_msgs = message.list(channel_id, alice['peer_id'], db)
        reactions = alice_msgs[0].get('reactions', [])
        assert len(reactions) == 2, f"Alice should see 2 reactions, got {len(reactions)}"

        emojis = sorted([r['emoji'] for r in reactions])
        assert '👍' in emojis, "Should have thumbs up"
        assert '❤️' in emojis, "Should have heart"

        thumbs_up = next(r for r in reactions if r['emoji'] == '👍')
        heart = next(r for r in reactions if r['emoji'] == '❤️')
        assert 'Alice' in thumbs_up['reactors'], "Alice should be thumbs up reactor"
        assert 'Bob' in heart['reactors'], "Bob should be heart reactor"

        bob_msgs = message.list(channel_id, bob['peer_id'], db)
        bob_reactions = bob_msgs[0].get('reactions', [])
        assert len(bob_reactions) == 2, "Bob should see 2 reactions"

        alice_reactions_sorted = sorted([r['emoji'] for r in reactions])
        bob_reactions_sorted = sorted([r['emoji'] for r in bob_reactions])
        assert alice_reactions_sorted == bob_reactions_sorted, "Reactions should converge"

    assert_eventually(both_see_both_reactions, db=db, start_t_ms=t_ms)


def test_message_reactions_removal(fresh_db):
    """Test: Alice removes her reaction and peers converge."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    channel_id = alice['channel_id']

    # Wait for Bob to see channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) == 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=None)

    # Create message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="React to me!",
        t_ms=t_ms,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Wait for Bob to see the message
    def bob_sees_message():
        bob_msgs = message.list(channel_id, bob['peer_id'], db)
        assert len(bob_msgs) > 0

    t_ms = assert_eventually(bob_sees_message, db=db, start_t_ms=t_ms)

    # Both add reactions
    alice_reaction = message_reaction.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        emoji='👍',
        t_ms=t_ms,
        db=db
    )
    bob_reaction = message_reaction.create(
        peer_id=bob['peer_id'],
        message_id=message_id,
        emoji='❤️',
        t_ms=t_ms + 100,
        db=db
    )
    db.commit()

    # Wait for both to see both reactions
    def both_see_both():
        alice_msgs = message.list(channel_id, alice['peer_id'], db)
        reactions = alice_msgs[0].get('reactions', [])
        assert len(reactions) == 2, f"Should have 2 reactions, got {len(reactions)}"

    t_ms = assert_eventually(both_see_both, db=db, start_t_ms=t_ms)

    # Alice removes her thumbs up reaction
    message_reaction.remove(
        peer_id=alice['peer_id'],
        message_id=message_id,
        emoji='👍',
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Verify Alice sees only heart
    alice_msgs = message.list(channel_id, alice['peer_id'], db)
    reactions = alice_msgs[0].get('reactions', [])
    assert len(reactions) == 1, f"Alice should see 1 reaction after removal, got {len(reactions)}"
    assert reactions[0]['emoji'] == '❤️', "Should only have heart"

    # Wait for Bob to see the removal
    def bob_sees_removal():
        bob_msgs = message.list(channel_id, bob['peer_id'], db)
        bob_reactions = bob_msgs[0].get('reactions', [])
        assert len(bob_reactions) == 1, f"Bob should see 1 reaction after sync, got {len(bob_reactions)}"
        assert bob_reactions[0]['emoji'] == '❤️', "Should only have heart"

    assert_eventually(bob_sees_removal, db=db, start_t_ms=t_ms)


def test_message_reactions_cascade_deletion(fresh_db):
    """Test: Deleting message cascade-deletes all reactions."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    channel_id = alice['channel_id']

    # Wait for Bob to see channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT DISTINCT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) == 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=None)

    # Create message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Delete me!",
        t_ms=t_ms,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Wait for Bob to see the message
    def bob_sees_message():
        bob_msgs = message.list(channel_id, bob['peer_id'], db)
        assert len(bob_msgs) > 0

    t_ms = assert_eventually(bob_sees_message, db=db, start_t_ms=t_ms)

    # Both add reactions
    message_reaction.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        emoji='👍',
        t_ms=t_ms,
        db=db
    )
    message_reaction.create(
        peer_id=bob['peer_id'],
        message_id=message_id,
        emoji='❤️',
        t_ms=t_ms + 100,
        db=db
    )
    db.commit()

    # Wait for both to see both reactions
    def both_see_both():
        alice_msgs = message.list(channel_id, alice['peer_id'], db)
        reactions = alice_msgs[0].get('reactions', [])
        assert len(reactions) == 2, "Should have 2 reactions before deletion"

    t_ms = assert_eventually(both_see_both, db=db, start_t_ms=t_ms)

    # Alice deletes message
    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Verify Alice sees no messages
    alice_msgs = message.list(channel_id, alice['peer_id'], db)
    assert len(alice_msgs) == 0, "Alice should see no messages after deletion"

    # Wait for Bob to see the deletion
    def bob_sees_deletion():
        bob_msgs = message.list(channel_id, bob['peer_id'], db)
        assert len(bob_msgs) == 0, "Bob should see no messages after deletion sync"

    assert_eventually(bob_sees_deletion, db=db, start_t_ms=t_ms)
