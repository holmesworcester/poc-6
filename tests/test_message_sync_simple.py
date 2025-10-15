"""Simple test for Alice-Bob message sync after bootstrap fix."""
import sqlite3
from db import Database
import schema
from events.transit import sync
from events.identity import user
from events.content import message


def test_alice_bob_message_sync():
    """Test that Alice and Bob can exchange messages after bootstrap."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Alice creates an invite for Bob
    from events.identity import invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Bootstrap - Bob sends multiple times to ensure Alice receives all events
    for t in [4000, 4050, 4100]:
        user.send_bootstrap_events(
            peer_id=bob['peer_id'],
            peer_shared_id=bob['peer_shared_id'],
            user_id=bob['user_id'],
            transit_prekey_shared_id=bob['transit_prekey_shared_id'],
            invite_data=bob['invite_data'],
            t_ms=t,
            db=db
        )
        # Alice receives after each send
        sync.receive(batch_size=20, t_ms=t + 10, db=db)

    # Initial sync rounds
    sync.send_request_to_all(t_ms=4400, db=db)
    for t in [4500, 4600]:
        sync.receive(batch_size=20, t_ms=t, db=db)

    sync.send_request_to_all(t_ms=4700, db=db)
    for t in [4800, 4900]:
        sync.receive(batch_size=20, t_ms=t, db=db)

    db.commit()

    # Ensure Bob has the channel projected before creating message
    # Keep syncing until Bob has it (with timeout)
    for attempt in range(20):
        bob_channel = db.query_one(
            "SELECT channel_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
            (bob['channel_id'], bob['peer_id'])
        )
        if bob_channel:
            break
        # More sync rounds
        t_base = 4950 + (attempt * 10)
        sync.send_request_to_all(t_ms=t_base, db=db)
        sync.receive(batch_size=20, t_ms=t_base + 5, db=db)

    assert bob_channel, f"Bob should have channel {bob['channel_id']} projected after sync"

    # Create messages
    alice_msg = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Hello from Alice',
        t_ms=5000,
        db=db
    )

    bob_msg = message.create(
        peer_id=bob['peer_id'],
        channel_id=bob['channel_id'],
        content='Hello from Bob',
        t_ms=6000,
        db=db
    )

    # Sync messages
    sync.send_request_to_all(t_ms=8000, db=db)
    sync.receive(batch_size=10, t_ms=9000, db=db)
    sync.receive(batch_size=100, t_ms=10000, db=db)
    sync.send_request_to_all(t_ms=11000, db=db)
    sync.receive(batch_size=10, t_ms=12000, db=db)
    sync.receive(batch_size=100, t_ms=13000, db=db)

    # Verify
    bob_msg_valid_for_alice = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob_msg['id'], alice['peer_id'])
    )
    alice_msg_valid_for_bob = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice_msg['id'], bob['peer_id'])
    )

    assert bob_msg_valid_for_alice, "Alice should have Bob's message"
    assert alice_msg_valid_for_bob, "Bob should have Alice's message"
