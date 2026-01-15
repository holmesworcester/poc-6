"""
Scenario test: Three players messaging (simplified with assert_eventually).

Demonstrates the clean testing pattern:
1. Setup peers and create events
2. Use assert_eventually to wait for sync
3. Assert on final state
"""
from events.identity import user, invite, peer
from events.content import message
from tests.utils.tick_helper import assert_eventually


def test_three_player_messaging_simple(fresh_db):
    """Alice and Bob share network, Charlie is separate."""
    db = fresh_db

    # === Setup ===
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)

    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    charlie = user.new_network(name='Charlie', t_ms=3000, db=db)

    db.commit()

    # === Wait for Bob to see Alice's channel ===
    def bob_has_channel():
        channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(channels) >= 1, f"Bob has {len(channels)} channels, expected 1"
        return channels[0]['channel_id']

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=4000, msg="Bob should see channel")

    # Verify it's the same channel as Alice
    bob_channel = bob_has_channel()
    assert bob_channel == alice['channel_id'], "Bob should share Alice's channel"

    # === Alice sends message ===
    alice_msg = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Hello from Alice!",
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # === Wait for Bob to see Alice's message ===
    def bob_sees_alice_message():
        msgs = message.list(alice['channel_id'], bob['peer_id'], db)
        contents = [m['content'] for m in msgs]
        assert "Hello from Alice!" in contents, f"Bob sees: {contents}"

    t_ms = assert_eventually(bob_sees_alice_message, db=db, start_t_ms=t_ms, msg="Bob should see Alice's message")

    # === Bob sends reply ===
    bob_msg = message.create(
        peer_id=bob['peer_id'],
        channel_id=alice['channel_id'],
        content="Hello from Bob!",
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # === Wait for Alice to see Bob's message ===
    def alice_sees_bob_message():
        msgs = message.list(alice['channel_id'], alice['peer_id'], db)
        contents = [m['content'] for m in msgs]
        assert "Hello from Bob!" in contents, f"Alice sees: {contents}"

    t_ms = assert_eventually(alice_sees_bob_message, db=db, start_t_ms=t_ms, msg="Alice should see Bob's message")

    # === Verify Charlie is isolated ===
    charlie_msgs = message.list(charlie['channel_id'], charlie['peer_id'], db)
    charlie_contents = [m['content'] for m in charlie_msgs]
    assert "Hello from Alice!" not in charlie_contents, "Charlie should NOT see Alice"
    assert "Hello from Bob!" not in charlie_contents, "Charlie should NOT see Bob"

    print("✅ Test passed!")
