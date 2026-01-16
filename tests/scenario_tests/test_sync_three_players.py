"""
Test sync convergence with three players.

Alice creates a network and invites Bob.
Charlie creates his own separate network.

Goal: Verify that all shareable events sync correctly between Alice and Bob,
      while Charlie remains isolated.
"""
from events.identity import user, invite, peer
from events.content import message
from tests.utils.tick_helper import assert_eventually


def test_sync_three_players_convergence(fresh_db):
    """Test that shareable events sync correctly between Alice and Bob."""
    db = fresh_db

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates an invite for Bob
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)

    # Bob joins Alice's network
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Charlie creates his own separate network
    charlie = user.new_network(name='Charlie', t_ms=3000, db=db)
    db.commit()

    # Get initial shareable counts
    alice_shareable_before = len(db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    ))
    bob_shareable_before = len(db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    ))

    # Wait for Bob to see Alice's channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=4000)

    # Send messages to verify sync works
    alice_msg = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Hello from Alice!',
        t_ms=t_ms,
        db=db
    )
    db.commit()

    bob_msg = message.create(
        peer_id=bob['peer_id'],
        channel_id=bob['channel_id'],
        content='Hello from Bob!',
        t_ms=t_ms + 100,
        db=db
    )
    db.commit()

    charlie_msg = message.create(
        peer_id=charlie['peer_id'],
        channel_id=charlie['channel_id'],
        content='Hello from Charlie!',
        t_ms=t_ms + 200,
        db=db
    )
    db.commit()

    # Wait for Alice to see Bob's message
    def alice_sees_bob_message():
        alice_messages = message.list(alice['channel_id'], alice['peer_id'], db)
        alice_contents = [m['content'] for m in alice_messages]
        assert 'Hello from Bob!' in alice_contents

    t_ms = assert_eventually(alice_sees_bob_message, db=db, start_t_ms=t_ms)

    # Wait for Bob to see Alice's message
    def bob_sees_alice_message():
        bob_messages = message.list(bob['channel_id'], bob['peer_id'], db)
        bob_contents = [m['content'] for m in bob_messages]
        assert 'Hello from Alice!' in bob_contents

    assert_eventually(bob_sees_alice_message, db=db, start_t_ms=t_ms)

    # Verify Charlie is isolated
    charlie_messages = message.list(charlie['channel_id'], charlie['peer_id'], db)
    charlie_contents = [m['content'] for m in charlie_messages]

    assert 'Hello from Charlie!' in charlie_contents, "Charlie should see his own message"
    assert 'Hello from Alice!' not in charlie_contents, "Charlie should NOT see Alice's message"
    assert 'Hello from Bob!' not in charlie_contents, "Charlie should NOT see Bob's message"

    # Verify Alice and Bob don't see Charlie's message
    alice_messages = message.list(alice['channel_id'], alice['peer_id'], db)
    alice_contents = [m['content'] for m in alice_messages]
    assert 'Hello from Charlie!' not in alice_contents, "Alice should NOT see Charlie's message"

    bob_messages = message.list(bob['channel_id'], bob['peer_id'], db)
    bob_contents = [m['content'] for m in bob_messages]
    assert 'Hello from Charlie!' not in bob_contents, "Bob should NOT see Charlie's message"
