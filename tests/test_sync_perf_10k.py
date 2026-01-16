"""
Performance test: Alice creates 10,000 messages, Bob syncs them.

This is a perf gut-check - verifies sync can handle bulk message transfer.
Run with: pytest -m slow
"""
import pytest
from events.identity import user, invite, peer
from events.content import message
from tests.utils.tick_helper import assert_eventually


@pytest.mark.slow
def test_sync_perf_10k(fresh_db):
    """Perf gut-check: sync 10k messages between two peers.

    Expected: ~60-90 seconds
    """
    db = fresh_db
    num_messages = 10000

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)

    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Alice creates messages in bulk
    print(f"\nCreating {num_messages} messages...")
    for i in range(num_messages):
        message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content=f'Message {i}',
            t_ms=3000 + i,
            db=db,
            return_latest=False
        )
        if (i + 1) % 2000 == 0:
            print(f"  {i + 1}/{num_messages}")
    db.commit()
    print(f"✓ Created {num_messages} messages")

    # Wait for Bob to receive all messages
    def bob_has_all_messages():
        count = db.query_one(
            "SELECT COUNT(*) as count FROM messages WHERE recorded_by = ?",
            (bob['peer_id'],)
        )['count']
        assert count >= num_messages, f"Bob has {count}/{num_messages} messages"

    print("Syncing...")
    assert_eventually(bob_has_all_messages, db=db, start_t_ms=20000, max_rounds=500)

    # Verify message content matches
    alice_ids = {r['message_id'] for r in db.query(
        "SELECT message_id FROM messages WHERE recorded_by = ?", (alice['peer_id'],)
    )}
    bob_ids = {r['message_id'] for r in db.query(
        "SELECT message_id FROM messages WHERE recorded_by = ?", (bob['peer_id'],)
    )}
    assert alice_ids == bob_ids, "Message IDs should match"

    print(f"✓ Bob received all {num_messages} messages")
