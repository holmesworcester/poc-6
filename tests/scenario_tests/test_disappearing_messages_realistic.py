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
from db import Database
import schema
from events.identity import user, invite, peer as peer_module
from events.content import channel, message, channel_update
from projection import dispatch
import purge_expired
import tick


def test_alice_creates_disappearing_channel_and_sends_messages():
    """Alice creates channel with disappearing messages and verifies expiration."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

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
    msg_result = message.send(
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


def test_alice_and_bob_see_messages_disappear_together():
    """Alice and Bob both see messages disappear at the same time."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Alice creates network, Bob joins ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    bob_peer_id = peer_module.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared_id = bob['peer_shared_id']
    db.commit()
    print("✓ Alice and Bob set up")

    # Sync phase: propagate group keys so Bob can decrypt Alice's channels
    print("\n=== Sync phase: propagate group keys (ticks 2100-5000) ===")
    for i in range(15):
        tick.tick(t_ms=2100 + i*200, db=db)
    db.commit()
    print("✓ Sync complete, Bob should now have group keys")

    # Create disappearing channel
    print("\n=== Alice creates channel with 3-second disappearing time ===")
    channel_id = channel.create(
        name='ephemeral',
        peer_id=alice['peer_id'],
        peer_shared_id=alice['peer_shared_id'],
        t_ms=3000,
        db=db,
        disappearing_time_ms=3000
    )
    db.commit()

    # Project channel for both
    channel.project_event(channel_id, alice['peer_id'], 3000, db)
    channel.project_event(channel_id, bob['peer_id'], 3500, db)
    db.commit()

    # Alice sends a message
    print("\n=== Alice sends message at t=4000 ===")
    msg_result = message.send(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="Secret message",
        t_ms=4000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Project message for both
    dispatch('message', message_id, alice['peer_id'], 4000, db)
    dispatch('message', message_id, bob['peer_id'], 4500, db)
    db.commit()

    # Both see the message
    alice_messages = message.list(channel_id, alice['peer_id'], db)
    bob_messages = message.list(channel_id, bob['peer_id'], db)
    assert len(alice_messages) == 1
    assert len(bob_messages) == 1
    assert alice_messages[0]['content'] == "Secret message"
    assert bob_messages[0]['content'] == "Secret message"
    print("✓ Both Alice and Bob can read the message")

    # Run purge at t=8000 (past expiry at 7000)
    print("\n=== Run purge_expired at t=8000 (past expiry) ===")
    cutoff_ms = 8000
    purge_expired.run_purge_expired_for_all_peers(cutoff_ms, db)
    db.commit()

    # Both should see empty message lists
    alice_messages_after = message.list(channel_id, alice['peer_id'], db)
    bob_messages_after = message.list(channel_id, bob['peer_id'], db)
    assert len(alice_messages_after) == 0, "Alice should see no messages after expiry"
    assert len(bob_messages_after) == 0, "Bob should see no messages after expiry"
    print("✓ Message disappeared for both Alice and Bob")

    print("\n✅ Multi-peer convergence test passed")


def test_channel_ttl_update_affects_new_messages():
    """Updating a channel's TTL affects new messages."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

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
    msg1 = message.send(
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
    channel_update.project_event(update_id, alice['peer_id'], 5000, db)
    db.commit()
    print("✓ Channel updated")

    # Send second message (should get 2-second TTL)
    print("\n=== Alice sends message 2 at t=6000 ===")
    msg2 = message.send(
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
