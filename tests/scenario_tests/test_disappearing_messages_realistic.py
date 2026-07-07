"""
Scenario test: Disappearing messages - messages expire and are purged at the right time.

Tests realistic disappearing message scenarios using API-style commands and queries only.
No direct database inspection - all verification via returned data and query functions.

The disappearing messages feature:
1. Channels have configurable TTLs (disappearing_time_ms)
2. Messages expire at created_at + disappearing_time_ms
3. Admins can update channel TTL via channel_update events
4. New messages inherit current channel TTL (lazy calculation)
5. Expired messages are automatically purged
6. Multi-peer convergence: all peers see same expiration times
"""
import sqlite3
from core.db import Database
from core import schema
from events.identity import user, invite, peer as peer_module
from events.content import channel, message, channel_update
from core import recorded
from core import purge_expired
from core import tick


def test_alice_creates_disappearing_channel_and_sends_messages(fresh_db):
    """Alice creates channel with disappearing messages and verifies expiration."""

    db = fresh_db

    print("\n=== Setup: Alice creates network ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Create a disappearing channel: messages expire after 5 seconds
    print("\n=== Alice creates channel with 5-second disappearing time ===")
    disappearing_time_ms = 5000
    ephemeral_channel_id = channel.create(
        name='ephemeral',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=2000,
        db=db,
        disappearing_time_ms=disappearing_time_ms
    )
    db.commit()
    print(f"✓ Channel created: {ephemeral_channel_id[:20]}...")

    # Send message at t=3000
    print("\n=== Alice sends message at t=3000 ===")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=ephemeral_channel_id,
        content="This will disappear in 5 seconds",
        t_ms=3000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()
    print(f"✓ Message created: {message_id[:20]}...")

    # Verify message appears in list before expiry
    print("\n=== Before expiry (t=7000): message should be visible ===")
    messages_before = message.list(ephemeral_channel_id, alice['peer_id'], db)
    assert len(messages_before) == 1, "Should have 1 message"
    assert messages_before[0]['content'] == "This will disappear in 5 seconds"
    assert messages_before[0]['message_id'] == message_id
    print("✓ Message visible in channel")

    # Message TTL should be 3000 + 5000 = 8000
    msg_ttl = messages_before[0].get('ttl_ms')
    if msg_ttl:
        assert msg_ttl == 8000, f"TTL should be 8000, got {msg_ttl}"
        print(f"✓ Message TTL correct: {msg_ttl}ms (expires at t=8000)")

    # Run purge_expired at t=8100 (past expiry)
    print("\n=== Run purge_expired at t=8100 (past expiry) ===")
    cutoff_ms = 8100
    purge_expired.run_purge_expired_for_all_peers(cutoff_ms, db)
    db.commit()
    print("✓ Purge ran")

    # After purge, message should be gone
    print("\n=== After expiry: message should be gone ===")
    messages_after = message.list(ephemeral_channel_id, alice['peer_id'], db)
    assert len(messages_after) == 0, "Message should be expired and deleted"
    print("✓ Message deleted after expiry")

    print("\n✅ Disappearing messages expiration test passed")


def test_alice_and_bob_see_messages_disappear_together(fresh_db):
    """Alice and Bob both see messages disappear at the same time."""
    from tests.utils.tick_helper import assert_eventually, initial_sync

    db = fresh_db

    print("\n=== Setup: Alice creates network, Bob joins ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    bob_peer_id = peer_module.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()
    print("✓ Alice and Bob set up")

    # Initial sync for group keys
    t_ms = initial_sync(db, start_t_ms=None)
    print("✓ Initial sync complete")

    # Create disappearing channel with short TTL
    disappearing_time_ms = 3000
    print(f"\n=== Alice creates channel with {disappearing_time_ms}ms disappearing time ===")
    channel_id = channel.create(
        name='ephemeral',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=t_ms,
        db=db,
        disappearing_time_ms=disappearing_time_ms
    )
    db.commit()
    channel_created_at = t_ms

    # Alice sends a message
    t_ms += 100
    print(f"\n=== Alice sends message at t={t_ms} ===")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Secret message",
        t_ms=t_ms,
        db=db
    )
    message_id = msg_result['id']
    message_created_at = t_ms
    db.commit()

    # Wait for Bob to receive the message via sync
    def bob_sees_message():
        bob_messages = message.list(channel_id, bob['peer_id'], db)
        assert len(bob_messages) == 1, f"Bob should have 1 message, got {len(bob_messages)}"
        assert bob_messages[0]['content'] == "Secret message"

    t_ms = assert_eventually(bob_sees_message, db=db, start_t_ms=t_ms)
    print("✓ Both Alice and Bob can read the message")

    # Both should have the message before TTL expires
    alice_messages = message.list(channel_id, alice['peer_id'], db)
    bob_messages = message.list(channel_id, bob['peer_id'], db)
    assert len(alice_messages) == 1
    assert len(bob_messages) == 1

    # Run purge after message expires (message_created_at + disappearing_time_ms)
    expiry_time = message_created_at + disappearing_time_ms
    purge_time = expiry_time + 100
    print(f"\n=== Run purge_expired at t={purge_time} (past expiry at {expiry_time}) ===")
    purge_expired.run_purge_expired_for_all_peers(purge_time, db)
    db.commit()

    # Both should see empty message lists
    alice_messages_after = message.list(channel_id, alice['peer_id'], db)
    bob_messages_after = message.list(channel_id, bob['peer_id'], db)
    assert len(alice_messages_after) == 0, "Alice should see no messages after expiry"
    assert len(bob_messages_after) == 0, "Bob should see no messages after expiry"
    print("✓ Message disappeared for both Alice and Bob")

    # Verify TRUE purge at database level (not just filtered views)
    from core.db import create_unsafe_db, create_safe_db
    unsafedb = create_unsafe_db(db)

    # Message blob should be truly purged from store
    blob_exists = unsafedb.query_one("SELECT 1 FROM store WHERE id = ?", (message_id,))
    assert blob_exists is None, "Message blob should be truly purged from store"

    # Verify message rows are truly deleted from messages table
    alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])

    alice_msg_row = alice_safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    bob_msg_row = bob_safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert alice_msg_row is None, "Message row should be deleted from Alice's messages table"
    assert bob_msg_row is None, "Message row should be deleted from Bob's messages table"

    # Note: TTL-expired messages are purged but NOT added to deleted_events
    # deleted_events is for explicit deletions (message_deletion events), not time-based expiry
    # This is by design - expired events just cease to exist

    print("✓ TRUE purge verified: blob deleted, message rows removed")

    print("\n✅ Multi-peer convergence test passed")


def test_channel_ttl_update_affects_new_messages(fresh_db):
    """Updating a channel's TTL affects new messages."""

    db = fresh_db

    print("\n=== Setup: Alice creates network ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Create channel with 10-second TTL
    print("\n=== Alice creates channel with 10-second TTL ===")
    channel_id = channel.create(
        name='flexible',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=2000,
        db=db,
        disappearing_time_ms=10000
    )
    db.commit()

    # Send first message
    print("\n=== Alice sends message 1 at t=3000 ===")
    msg1 = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Message 1",
        t_ms=3000,
        db=db
    )
    db.commit()

    # Update channel TTL to 2 seconds
    print("\n=== Alice updates channel to 2-second TTL ===")
    update_id = channel_update.create(
        channel_id=channel_id,
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=5000,
        db=db,
        new_disappearing_time_ms=2000
    )
    # Projection happens automatically via store.event() in create()
    db.commit()
    print("✓ Channel updated")

    # Send second message (should get 2-second TTL)
    print("\n=== Alice sends message 2 at t=6000 ===")
    msg2 = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Message 2",
        t_ms=6000,
        db=db
    )
    db.commit()

    # Both messages should be visible before selective expiry
    all_messages = message.list(channel_id, alice['peer_id'], db)
    assert len(all_messages) == 2, "Should have 2 messages"
    print("✓ Both messages visible")

    # Purge at t=9000 (expires msg2 with TTL=8000, but not msg1 with TTL=13000)
    print("\n=== Purge at t=9000 (selective expiry) ===")
    cutoff_ms = 9000
    purge_expired.run_purge_expired_for_all_peers(cutoff_ms, db)
    db.commit()

    # Only message 1 should remain
    remaining_messages = message.list(channel_id, alice['peer_id'], db)
    assert len(remaining_messages) == 1, "Only message 1 should remain"
    assert remaining_messages[0]['content'] == "Message 1"
    print("✓ Message 2 expired, message 1 still exists")

    print("\n✅ Channel TTL update test passed")
