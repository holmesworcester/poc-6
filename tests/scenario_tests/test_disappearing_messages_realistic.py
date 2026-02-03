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
from tests.utils.tick_helper import TestClock


def test_alice_creates_disappearing_channel_and_sends_messages(fresh_db):
    """Alice creates channel with disappearing messages and verifies expiration."""

    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Alice creates network ===")
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    print("\n=== Alice creates channel with 5-second disappearing time ===")
    disappearing_time_ms = 5000
    ephemeral_channel_id = channel.create(
        name='ephemeral',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=clock.tick(),
        db=db,
        disappearing_time_ms=disappearing_time_ms
    )
    db.commit()
    print(f"✓ Channel created: {ephemeral_channel_id[:20]}...")

    print("\n=== Alice sends message ===")
    msg_created_at = clock.tick()
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=ephemeral_channel_id,
        content="This will disappear in 5 seconds",
        t_ms=msg_created_at,
        db=db
    )
    message_id = msg_result['id']
    db.commit()
    print(f"✓ Message created: {message_id[:20]}...")

    print("\n=== Before expiry: message should be visible ===")
    messages_before = message.list(ephemeral_channel_id, alice['peer_id'], db)
    assert len(messages_before) == 1, "Should have 1 message"
    assert messages_before[0]['content'] == "This will disappear in 5 seconds"
    assert messages_before[0]['message_id'] == message_id
    print("✓ Message visible in channel")

    expected_ttl = msg_created_at + disappearing_time_ms
    msg_ttl = messages_before[0].get('ttl_ms')
    if msg_ttl:
        assert msg_ttl == expected_ttl, f"TTL should be {expected_ttl}, got {msg_ttl}"
        print(f"✓ Message TTL correct: {msg_ttl}ms")

    print("\n=== Run purge_expired (past expiry) ===")
    cutoff_ms = expected_ttl + 100
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

    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Alice creates network, Bob joins ===")
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)

    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )

    bob_peer_id = peer_module.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.tick(), db=db)
    bob_peer_shared_id = bob['peer_shared_id']
    db.commit()
    print("✓ Alice and Bob set up")

    print("\n=== Sync phase: propagate group keys ===")
    for i in range(15):
        tick.tick(t_ms=clock.tick(), db=db)
    db.commit()
    print("✓ Sync complete, Bob should now have group keys")

    print("\n=== Alice creates channel with 3-second disappearing time ===")
    disappearing_time_ms = 3000
    channel_id = channel.create(
        name='ephemeral',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=clock.tick(),
        db=db,
        disappearing_time_ms=disappearing_time_ms
    )
    db.commit()

    bob_recorded_id = recorded.create(channel_id, bob['peer_id'], clock.tick(), db, return_dupes=False)
    recorded.project(bob_recorded_id, db)
    db.commit()

    print("\n=== Alice sends message ===")
    msg_created_at = clock.tick()
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Secret message",
        t_ms=msg_created_at,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    bob_msg_recorded_id = recorded.create(message_id, bob['peer_id'], clock.tick(), db, return_dupes=False)
    recorded.project(bob_msg_recorded_id, db)
    db.commit()

    alice_messages = message.list(channel_id, alice['peer_id'], db)
    bob_messages = message.list(channel_id, bob['peer_id'], db)
    assert len(alice_messages) == 1
    assert len(bob_messages) == 1
    assert alice_messages[0]['content'] == "Secret message"
    assert bob_messages[0]['content'] == "Secret message"
    print("✓ Both Alice and Bob can read the message")

    print("\n=== Run purge_expired (past expiry) ===")
    cutoff_ms = msg_created_at + disappearing_time_ms + 1000
    purge_expired.run_purge_expired_for_all_peers(cutoff_ms, db)
    db.commit()

    # Both should see empty message lists
    alice_messages_after = message.list(channel_id, alice['peer_id'], db)
    bob_messages_after = message.list(channel_id, bob['peer_id'], db)
    assert len(alice_messages_after) == 0, "Alice should see no messages after expiry"
    assert len(bob_messages_after) == 0, "Bob should see no messages after expiry"
    print("✓ Message disappeared for both Alice and Bob")

    print("\n✅ Multi-peer convergence test passed")


def test_channel_ttl_update_affects_new_messages(fresh_db):
    """Updating a channel's TTL affects new messages."""

    db = fresh_db
    clock = TestClock()

    print("\n=== Setup: Alice creates network ===")
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    print("\n=== Alice creates channel with 10-second TTL ===")
    original_ttl = 10000
    channel_id = channel.create(
        name='flexible',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=clock.tick(),
        db=db,
        disappearing_time_ms=original_ttl
    )
    db.commit()

    print("\n=== Alice sends message 1 ===")
    msg1_created_at = clock.tick()
    msg1 = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Message 1",
        t_ms=msg1_created_at,
        db=db
    )
    db.commit()

    print("\n=== Alice updates channel to 2-second TTL ===")
    new_ttl = 2000
    update_id = channel_update.create(
        channel_id=channel_id,
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=clock.tick(),
        db=db,
        new_disappearing_time_ms=new_ttl
    )
    db.commit()
    print("✓ Channel updated")

    print("\n=== Alice sends message 2 ===")
    msg2_created_at = clock.tick()
    msg2 = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Message 2",
        t_ms=msg2_created_at,
        db=db
    )
    db.commit()

    all_messages = message.list(channel_id, alice['peer_id'], db)
    assert len(all_messages) == 2, "Should have 2 messages"
    print("✓ Both messages visible")

    print("\n=== Purge (selective expiry) ===")
    cutoff_ms = msg2_created_at + new_ttl + 1000
    purge_expired.run_purge_expired_for_all_peers(cutoff_ms, db)
    db.commit()

    # Only message 1 should remain
    remaining_messages = message.list(channel_id, alice['peer_id'], db)
    assert len(remaining_messages) == 1, "Only message 1 should remain"
    assert remaining_messages[0]['content'] == "Message 1"
    print("✓ Message 2 expired, message 1 still exists")

    print("\n✅ Channel TTL update test passed")
