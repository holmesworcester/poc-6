"""
TreeKEM Forward Secrecy Tests.

Tests the forward secrecy mechanism in the sender key model with TreeKEM:
- Messages can be deleted and encryption keys purged
- Newcomers can still access message history (keys preserved for existing messages)
- Keys are forgotten (purged) after message deletion

These tests follow RULES.md patterns:
- Use TestClock for consistent timestamps
- Use assert_eventually for convergence
- Query via API, not direct table inspection
"""

import pytest
import sqlite3
from core.db import Database, create_safe_db, create_unsafe_db
from core import schema
from core import crypto
from events.identity import user, peer, invite
from events.content import message, message_deletion
from events.group import secret, sender_key
from tests.utils.tick_helper import assert_eventually, TestClock


def get_message_key_id(message_id: str, peer_id: str, db) -> str | None:
    """Helper to get the key_id used to encrypt a message."""
    safedb = create_safe_db(db, recorded_by=peer_id)
    row = safedb.query_one(
        "SELECT key_id FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, peer_id)
    )
    return row['key_id'] if row else None


def test_message_deletion_marks_key_for_purge(fresh_db):
    """When a message is deleted, its encryption key is marked for purging."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Secret message to be deleted",
        t_ms=clock.tick(),
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Get the key_id from the stored message
    key_id = get_message_key_id(message_id, alice['peer_id'], db)
    assert key_id is not None, "Message should have a key_id"

    # Verify key exists
    key_bytes = secret.get_key_bytes(key_id, alice['peer_id'], db)
    assert key_bytes is not None, "Key should exist before deletion"

    # Alice deletes the message
    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Verify key is marked for purging
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    purge_marker = safedb.query_one(
        "SELECT 1 FROM keys_to_purge WHERE key_id = ? AND recorded_by = ?",
        (key_id, alice['peer_id'])
    )
    assert purge_marker is not None, "Key should be marked for purging after deletion"


def test_purge_cycle_removes_orphan_keys(fresh_db):
    """After purge cycle, keys with no remaining messages are removed."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message to delete",
        t_ms=clock.tick(),
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Get the key_id from the stored message
    key_id = get_message_key_id(message_id, alice['peer_id'], db)
    assert key_id is not None

    # Verify key exists
    key_bytes_before = secret.get_key_bytes(key_id, alice['peer_id'], db)
    assert key_bytes_before is not None, "Key should exist before purge"

    # Delete the message
    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Run purge cycle
    stats = message_deletion.run_message_purge_cycle(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Key should be purged (no messages use it anymore)
    key_bytes_after = secret.get_key_bytes(key_id, alice['peer_id'], db)
    assert key_bytes_after is None, "Key should be purged after cycle"
    assert stats['keys_purged'] >= 1, "Should have purged at least one key"


def test_purge_cycle_preserves_undeleted_messages(fresh_db):
    """After purge cycle, remaining (undeleted) messages are still readable."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Alice sends two messages
    msg1_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message one - will be deleted",
        t_ms=clock.tick(),
        db=db
    )
    msg1_id = msg1_result['id']
    db.commit()

    msg2_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message two - will remain",
        t_ms=clock.tick(),
        db=db
    )
    msg2_id = msg2_result['id']
    db.commit()

    # Delete message 1
    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=msg1_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Run purge cycle (this rekeys message 2 if it used the same key)
    message_deletion.run_message_purge_cycle(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Message 2 should still be accessible
    messages = message.list(alice['channel_id'], alice['peer_id'], db)
    assert len(messages) == 1, "Should have exactly one message"
    assert messages[0]['content'] == "Message two - will remain"


def test_newcomer_can_access_history(fresh_db):
    """A newcomer joining can access message history (basic case without purge)."""
    db = fresh_db
    clock = TestClock()

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message for newcomer",
        t_ms=clock.tick(),
        db=db
    )
    msg_id = msg_result['id']
    db.commit()

    # Bob joins as newcomer
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Wait for Bob to see the channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=clock.now())

    # Wait for Bob to see the message
    def bob_sees_message():
        bob_messages = message.list(alice['channel_id'], bob['peer_id'], db)
        contents = [m['content'] for m in bob_messages]
        assert "Message for newcomer" in contents, f"Bob should see message, got: {contents}"

    assert_eventually(bob_sees_message, db=db, start_t_ms=t_ms)


def test_deleted_message_not_visible_to_newcomer(fresh_db):
    """A newcomer joining after deletion cannot see deleted messages, but can see other messages."""
    db = fresh_db
    clock = TestClock()

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Alice sends TWO messages - one will be deleted, one will remain
    # This ensures we're testing deletion visibility, not just empty message lists
    msg_to_delete = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message to be deleted",
        t_ms=clock.tick(),
        db=db
    )
    msg_deleted_id = msg_to_delete['id']
    db.commit()

    msg_to_keep = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message to keep visible",
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Alice deletes the first message
    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=msg_deleted_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Bob joins as newcomer
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Wait for Bob to see the channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=clock.now())

    # Wait for Bob to see the kept message (proves sync is working)
    def bob_sees_kept_message():
        bob_messages = message.list(alice['channel_id'], bob['peer_id'], db)
        contents = [m['content'] for m in bob_messages]
        assert "Message to keep visible" in contents, f"Bob should see kept message, got: {contents}"
        # And verify deleted message is NOT there
        assert "Message to be deleted" not in contents, f"Bob should NOT see deleted message, got: {contents}"

    assert_eventually(bob_sees_kept_message, db=db, start_t_ms=t_ms)


def test_purged_key_cannot_decrypt_old_content(fresh_db):
    """After purge, the old key material is gone and cannot decrypt anything."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message to delete",
        t_ms=clock.tick(),
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Get the key_id from the stored message
    key_id = get_message_key_id(message_id, alice['peer_id'], db)
    assert key_id is not None

    # Store the key bytes for verification
    key_bytes_before = secret.get_key_bytes(key_id, alice['peer_id'], db)
    assert key_bytes_before is not None

    # Delete and purge
    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    message_deletion.run_message_purge_cycle(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Key bytes should be gone
    key_bytes_after = secret.get_key_bytes(key_id, alice['peer_id'], db)
    assert key_bytes_after is None, "Purged key should not be retrievable"


def test_multiple_deletions_single_purge_cycle(fresh_db):
    """Multiple message deletions are handled in a single purge cycle."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Alice sends several messages
    message_ids = []
    key_ids = set()
    for i in range(5):
        msg_result = message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content=f"Message {i}",
            t_ms=clock.tick(),
            db=db
        )
        message_ids.append(msg_result['id'])
    db.commit()

    # Get key_ids for all messages
    for msg_id in message_ids:
        key_id = get_message_key_id(msg_id, alice['peer_id'], db)
        if key_id:
            key_ids.add(key_id)

    # Delete all messages
    for msg_id in message_ids:
        message_deletion.create(
            peer_id=alice['peer_id'],
            message_id=msg_id,
            t_ms=clock.tick(),
            db=db
        )
    db.commit()

    # Run single purge cycle
    stats = message_deletion.run_message_purge_cycle(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # All keys should be purged
    for key_id in key_ids:
        key_bytes = secret.get_key_bytes(key_id, alice['peer_id'], db)
        assert key_bytes is None, f"Key {key_id[:20]}... should be purged"

    # Verify no messages remain
    messages = message.list(alice['channel_id'], alice['peer_id'], db)
    assert len(messages) == 0, "All messages should be deleted"


def test_multi_peer_purge_convergence(fresh_db):
    """Multiple peers converge to same purge state after sync."""
    db = fresh_db
    clock = TestClock()

    # Alice creates network
    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Bob joins
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Wait for Bob to see channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=clock.now())

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message to be deleted",
        t_ms=t_ms,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Wait for Bob to see message
    def bob_sees_message():
        bob_messages = message.list(alice['channel_id'], bob['peer_id'], db)
        assert any(m['message_id'] == message_id for m in bob_messages)

    t_ms = assert_eventually(bob_sees_message, db=db, start_t_ms=t_ms)

    # Alice deletes the message
    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Wait for Bob to see deletion (message gone)
    def bob_sees_deletion():
        bob_messages = message.list(alice['channel_id'], bob['peer_id'], db)
        assert all(m['message_id'] != message_id for m in bob_messages), "Bob should not see deleted message"

    t_ms = assert_eventually(bob_sees_deletion, db=db, start_t_ms=t_ms)

    # Both peers run purge cycle
    alice_stats = message_deletion.run_message_purge_cycle(
        peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    bob_stats = message_deletion.run_message_purge_cycle(
        peer_id=bob['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # At least one peer should have purged the key (Alice created it, so she has it)
    total_purged = alice_stats['keys_purged'] + bob_stats['keys_purged']
    assert total_purged >= 1, f"At least one peer should have purged the key, got alice={alice_stats['keys_purged']}, bob={bob_stats['keys_purged']}"

    # Verify no messages remain for either peer
    alice_messages = message.list(alice['channel_id'], alice['peer_id'], db)
    bob_messages = message.list(alice['channel_id'], bob['peer_id'], db)
    assert len(alice_messages) == 0, "Alice should have no messages"
    assert len(bob_messages) == 0, "Bob should have no messages"


def test_partial_deletion_preserves_remaining_message(fresh_db):
    """Deleting one message doesn't affect other messages - message 2 remains readable."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Create two messages
    msg1 = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message 1 - will be deleted",
        t_ms=clock.tick(),
        db=db
    )
    msg1_id = msg1['id']
    db.commit()

    msg2 = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message 2 - will remain",
        t_ms=clock.tick(),
        db=db
    )
    msg2_id = msg2['id']
    db.commit()

    # Verify both messages exist before deletion
    messages_before = message.list(alice['channel_id'], alice['peer_id'], db)
    assert len(messages_before) == 2, f"Should have 2 messages before deletion, got {len(messages_before)}"

    # Get key_ids for both messages
    msg1_key_id = get_message_key_id(msg1_id, alice['peer_id'], db)
    msg2_key_id_before = get_message_key_id(msg2_id, alice['peer_id'], db)
    assert msg1_key_id is not None, "Message 1 should have a key"
    assert msg2_key_id_before is not None, "Message 2 should have a key"

    # Delete only message 1
    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=msg1_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Run purge cycle
    stats = message_deletion.run_message_purge_cycle(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # THE CRITICAL INVARIANT: Message 2 should still be readable
    # This is the actual forward secrecy guarantee - deletion of one message
    # doesn't destroy access to other messages
    messages_after = message.list(alice['channel_id'], alice['peer_id'], db)
    assert len(messages_after) == 1, f"Should have exactly one message remaining, got {len(messages_after)}"
    assert messages_after[0]['content'] == "Message 2 - will remain", \
        f"Remaining message should be message 2, got: {messages_after[0]['content']}"

    # Verify the deleted message's key was purged
    msg1_key_bytes_after = secret.get_key_bytes(msg1_key_id, alice['peer_id'], db)
    assert msg1_key_bytes_after is None, "Deleted message's key should be purged"
    assert stats['keys_purged'] >= 1, "Should have purged at least one key"


if __name__ == "__main__":
    # Run tests directly for debugging
    import sqlite3
    from core.db import Database

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    test_message_deletion_marks_key_for_purge(db)
    print("test_message_deletion_marks_key_for_purge passed")
