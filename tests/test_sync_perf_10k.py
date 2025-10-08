"""
Performance test: Alice creates 10,000 messages, Bob syncs them.

Tracks how many sync steps (sync.sync_all() + sync.receive() calls) are needed
to transfer all messages from Alice to Bob, using the realistic user.join() API.
"""
import sqlite3
import logging
from db import Database
import schema
from events import message, sync, user

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
    from events import invite
    invite_id, invite_link, invite_data = invite.create(
        inviter_peer_id=alice['peer_id'],
        inviter_peer_shared_id=alice['peer_shared_id'],
        group_id=alice['group_id'],
        channel_id=alice['channel_id'],
        key_id=alice['key_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins Alice's network via invite
    log.info("Bob joining Alice's network...")
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_id = bob['peer_id']
    bob_peer_shared_id = bob['peer_shared_id']
    bob_user_id = bob['user_id']
    bob_key_id = bob['key_id']
    bob_group_id = bob['group_id']
    bob_channel_id = alice_channel_id  # Same channel as Alice

    # Bootstrap: Bob sends bootstrap events + sync request
    log.info("Bob sending bootstrap events...")
    user.send_bootstrap_events(
        peer_id=bob_peer_id,
        peer_shared_id=bob_peer_shared_id,
        user_id=bob_user_id,
        prekey_shared_id=bob['prekey_shared_id'],
        invite_data=bob['invite_data'],
        t_ms=4000,
        db=db
    )

    # Initial sync rounds to establish connection
    log.info("Running initial sync rounds to establish connection...")
    sync.receive(batch_size=20, t_ms=4100, db=db)  # Alice receives Bob's bootstrap
    sync.receive(batch_size=20, t_ms=4200, db=db)  # Alice sends sync response
    sync.receive(batch_size=20, t_ms=4300, db=db)  # Bob receives Alice's response

    # Continue bloom sync to exchange remaining events
    sync.sync_all(t_ms=4400, db=db)
    sync.receive(batch_size=20, t_ms=4500, db=db)
    sync.receive(batch_size=20, t_ms=4600, db=db)

    # Additional sync rounds to ensure all events flow through
    sync.sync_all(t_ms=4700, db=db)
    sync.receive(batch_size=20, t_ms=4800, db=db)
    sync.receive(batch_size=20, t_ms=4900, db=db)

    db.commit()
    log.info("Bootstrap complete - Alice and Bob are connected")

    # Alice creates 10,000 messages
    log.info("Alice creating 10,000 messages...")
    num_messages = 10000
    for i in range(num_messages):
        message.create_message(
            params={
                'content': f'Message {i}',
                'channel_id': alice_channel_id,
                'group_id': alice_group_id,
                'peer_id': alice_peer_id,
                'peer_shared_id': alice_peer_shared_id,
                'key_id': alice_key_id
            },
            t_ms=10000 + i,
            db=db
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

    # Now sync: track how many sync steps it takes for Bob to get all messages
    log.info("\nStarting sync performance test...")
    sync_step = 0
    max_steps = 500  # Safety limit

    while sync_step < max_steps:
        sync_step += 1

        # Step: All peers send sync requests
        sync.sync_all(t_ms=30000 + sync_step * 100, db=db)

        # Step: Receive sync requests (unwraps and auto-sends responses)
        sync.receive(batch_size=100, t_ms=30000 + sync_step * 100 + 10, db=db)

        # Step: Receive sync responses
        sync.receive(batch_size=100, t_ms=30000 + sync_step * 100 + 20, db=db)

        # Check Bob's message count
        bob_messages = db.query(
            "SELECT COUNT(*) as count FROM messages WHERE recorded_by = ?",
            (bob_peer_id,)
        )
        bob_msg_count = bob_messages[0]['count']

        if sync_step % 10 == 0 or bob_msg_count >= num_messages:
            log.info(f"  Step {sync_step}: Bob has {bob_msg_count}/{num_messages} messages")

        # Check if sync is complete
        if bob_msg_count >= num_messages:
            log.info(f"\n✓ Sync complete after {sync_step} sync steps!")
            break

        # Check if sync has stalled (no progress and no queued blobs)
        # Note: We don't track prev_count here, we just check if queue is empty
        # If Bob hasn't received all messages yet and queue is empty, we're stalled
        if sync_step > 10 and bob_msg_count < num_messages:
            # Check if there are any events in the incoming queue
            queue_count = db.query_one(
                "SELECT COUNT(*) as count FROM incoming_blobs"
            )
            if queue_count and queue_count['count'] == 0:
                log.warning(f"Sync stalled at step {sync_step}: Bob has {bob_msg_count}/{num_messages} messages, queue empty")
                break

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
    log.info(f"Sync steps taken:        {sync_step}")
    log.info(f"Messages match:          {alice_msg_ids == bob_msg_ids}")
    log.info("="*60)

    # Assertions
    assert alice_final_count == num_messages, f"Alice should have {num_messages} messages, has {alice_final_count}"
    assert bob_final_count == num_messages, f"Bob should have {num_messages} messages, has {bob_final_count}"
    assert alice_msg_ids == bob_msg_ids, "Alice and Bob should have the same message list"
    assert sync_step > 0, "Should take at least 1 sync step"
    assert sync_step < max_steps, f"Sync took too many steps ({sync_step}), something is wrong"

    log.info("\n✓ All assertions passed! Sync performance test successful.")


if __name__ == '__main__':
    test_sync_perf_10k()
