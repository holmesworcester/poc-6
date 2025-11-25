"""
Performance test: Alice creates 10,000 messages, Bob syncs them.

Tracks how many tick() calls are needed to transfer all messages from
Alice to Bob, using the realistic user.join() API.
"""
import sqlite3
import logging
from db import Database
import schema
import tick
from events.identity import user, peer
from events.content import message

# Enable logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def test_sync_perf_10k():
    """Test sync performance: Alice creates 10k messages, Bob syncs them."""

    # Setup: Initialize in-memory database
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Alice creates a network
    log.info("Creating Alice's network...")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    alice_peer_id = alice['peer_id']
    alice_peer_shared_id = alice['peer_shared_id']
    alice_key_id = alice['key_id']
    alice_group_id = alice['group_id']
    alice_channel_id = alice['channel_id']

    # Alice creates an invite for Bob
    from events.identity import invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins Alice's network via invite
    log.info("Bob joining Alice's network...")
    bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)

    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_id = bob['peer_id']
    bob_peer_shared_id = bob['peer_shared_id']
    bob_user_id = bob['user_id']
    bob_key_id = bob['key_id']
    bob_group_id = bob['group_id']
    bob_channel_id = alice_channel_id  # Same channel as Alice

    # Bootstrap: Initial sync rounds to establish connection using tick()
    log.info("Running initial sync rounds to establish connection...")
    for i in range(5):
        tick.tick(t_ms=4000 + i*200, db=db)

    db.commit()
    log.info("Bootstrap complete - Alice and Bob are connected")

    # Alice creates 10,000 messages
    log.info("Alice creating 10,000 messages...")
    num_messages = 10000
    for i in range(num_messages):
        message.create(
            peer_id=alice_peer_id,
            channel_id=alice_channel_id,
            content=f'Message {i}',
            t_ms=10000 + i,
            db=db,
            return_latest=False  # Skip fetching messages for bulk creation performance
        )

        if (i + 1) % 1000 == 0:
            log.info(f"  Created {i + 1} messages")

    db.commit()
    log.info(f"Alice created {num_messages} messages")

    # Check Alice's message count
    alice_messages = db.query(
        "SELECT COUNT(*) as count FROM messages WHERE recorded_by = ?",
        (alice_peer_id,)
    )
    alice_msg_count = alice_messages[0]['count']
    log.info(f"Alice has {alice_msg_count} messages in messages table")

    # Now sync: track how many ticks it takes for Bob to get all messages
    log.info("\nStarting sync performance test...")
    tick_count = 0
    max_ticks = 500  # Safety limit

    while tick_count < max_ticks:
        tick_count += 1

        # Run tick (handles all sync jobs with configured batch_size)
        tick.tick(t_ms=30000 + tick_count * 100, db=db)

        # Check Bob's message count periodically
        if tick_count % 10 == 0:
            bob_messages = db.query(
                "SELECT COUNT(*) as count FROM messages WHERE recorded_by = ?",
                (bob_peer_id,)
            )
            bob_msg_count = bob_messages[0]['count']

            log.info(f"  Tick {tick_count}: Bob has {bob_msg_count}/{num_messages} messages")

            # Check if sync is complete
            if bob_msg_count >= num_messages:
                log.info(f"\n✓ Sync complete after {tick_count} ticks!")
                break

            # Check if sync has stalled
            if tick_count > 10 and bob_msg_count < num_messages:
                queue_count = db.query_one(
                    "SELECT COUNT(*) as count FROM incoming_blobs"
                )
                if queue_count and queue_count['count'] == 0:
                    log.warning(f"Sync stalled at tick {tick_count}: Bob has {bob_msg_count}/{num_messages} messages, queue empty")
                    break

    # Final check
    bob_messages = db.query(
        "SELECT COUNT(*) as count FROM messages WHERE recorded_by = ?",
        (bob_peer_id,)
    )
    bob_msg_count = bob_messages[0]['count']
    sync_step = tick_count  # For backwards compatibility with assertions

    db.commit()

    # Final verification
    alice_final_messages = db.query(
        "SELECT COUNT(*) as count FROM messages WHERE recorded_by = ?",
        (alice_peer_id,)
    )
    bob_final_messages = db.query(
        "SELECT COUNT(*) as count FROM messages WHERE recorded_by = ?",
        (bob_peer_id,)
    )

    alice_final_count = alice_final_messages[0]['count']
    bob_final_count = bob_final_messages[0]['count']

    # Get actual message lists to verify they match
    alice_message_list = db.query(
        "SELECT message_id FROM messages WHERE recorded_by = ? ORDER BY created_at",
        (alice_peer_id,)
    )
    bob_message_list = db.query(
        "SELECT message_id FROM messages WHERE recorded_by = ? ORDER BY created_at",
        (bob_peer_id,)
    )

    alice_msg_ids = {row['message_id'] for row in alice_message_list}
    bob_msg_ids = {row['message_id'] for row in bob_message_list}

    # Print performance summary
    log.info("\n" + "="*60)
    log.info("SYNC PERFORMANCE SUMMARY")
    log.info("="*60)
    log.info(f"Messages created:        {num_messages}")
    log.info(f"Alice message count:     {alice_final_count}")
    log.info(f"Bob message count:       {bob_final_count}")
    log.info(f"Ticks taken:             {tick_count}")
    log.info(f"Messages match:          {alice_msg_ids == bob_msg_ids}")
    log.info("="*60)

    # Assertions
    assert alice_final_count == num_messages, f"Alice should have {num_messages} messages, has {alice_final_count}"
    assert bob_final_count == num_messages, f"Bob should have {num_messages} messages, has {bob_final_count}"
    assert alice_msg_ids == bob_msg_ids, "Alice and Bob should have the same message list"
    assert tick_count > 0, "Should take at least 1 tick"
    assert tick_count < max_ticks, f"Sync took too many ticks ({tick_count}), something is wrong"

    log.info("\n✓ All assertions passed! Sync performance test successful.")


if __name__ == '__main__':
    test_sync_perf_10k()
