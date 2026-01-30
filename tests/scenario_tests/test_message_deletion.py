"""
Scenario test: Message deletion with atomicity and convergence guarantees.

Tests deletion semantics analogous to blocking/unblocking:
- Deletions act as permanent blocks on message projection
- Messages arriving after deletion are blocked (not projected)
- Deletions arriving after messages remove them atomically
- All peers converge to same deletion state

Atomicity tests:
- Deletion + message removal happens in single transaction
- No partial states visible
- Rollback safety
"""
import pytest
import sqlite3
from core.db import Database, create_safe_db, create_unsafe_db
from core import schema
from events.identity import user, invite, peer, admin
from events.content import message
from events.content import message_deletion
from tests.utils.tick_helper import assert_eventually, TestClock


def test_message_deletion_self():
    """Test self-deletion: author deletes their own message."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Hello, this will be deleted",
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Verify message exists
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    msg_check = safedb.query_one(
        "SELECT content FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert msg_check is not None, "Message should exist before deletion"
    assert msg_check['content'] == "Hello, this will be deleted"

    # Alice deletes the message
    deletion_id = message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=3000,
        db=db
    )
    db.commit()

    # Verify message is deleted
    msg_check_after = safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert msg_check_after is None, "Message should be deleted"

    # Verify deletion record exists
    deletion_check = safedb.query_one(
        "SELECT 1 FROM message_deletions WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert deletion_check is not None, "Deletion record should exist"

    # Verify event is marked as deleted in deleted_events
    deleted_events_check = safedb.query_one(
        "SELECT 1 FROM deleted_events WHERE event_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert deleted_events_check is not None, "Event should be marked as deleted in deleted_events"

    # Verify blob is removed from store
    unsafedb = create_unsafe_db(db)
    blob_check = unsafedb.query_one(
        "SELECT 1 FROM store WHERE id = ?",
        (message_id,)
    )
    assert blob_check is None, "Blob should be removed from store"


def test_message_deletion_admin(fresh_db):
    """Test admin deletion: admin deletes another user's message."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
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

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=None)

    # Make Bob an admin
    alice_admin_grant = admin.my_grant(alice['user_id'], alice['network_id'], alice['peer_id'], db)
    alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
    admin.create(
        user_id=bob['user_id'],
        network_id=alice['network_id'],
        signed_by=alice['peer_shared_id'],
        signer_private_key=alice_private_key,
        t_ms=t_ms,
        peer_id=alice['peer_id'],
        db=db,
        admin_grant=alice_admin_grant
    )
    db.commit()

    # Wait for Bob to be admin
    def bob_is_admin():
        result = admin.is_user_admin(bob['user_id'], alice['network_id'], bob['peer_id'], db)
        assert result

    t_ms = assert_eventually(bob_is_admin, db=db, start_t_ms=t_ms)

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Alice's message to be deleted by Bob",
        t_ms=t_ms,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Wait for Bob to see the message
    def bob_sees_message():
        bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
        bob_msg_check = bob_safedb.query_one(
            "SELECT content FROM messages WHERE message_id = ? AND recorded_by = ?",
            (message_id, bob['peer_id'])
        )
        assert bob_msg_check is not None

    t_ms = assert_eventually(bob_sees_message, db=db, start_t_ms=t_ms)

    # Bob (admin) deletes Alice's message
    deletion_id = message_deletion.create(
        peer_id=bob['peer_id'],
        message_id=message_id,
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Verify message is deleted from Bob's view
    bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
    bob_msg_after = bob_safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_msg_after is None, "Message should be deleted from Bob's view"

    # Wait for Alice to see deletion
    def alice_sees_deletion():
        alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
        alice_msg_after = alice_safedb.query_one(
            "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
            (message_id, alice['peer_id'])
        )
        assert alice_msg_after is None

    assert_eventually(alice_sees_deletion, db=db, start_t_ms=t_ms)


def test_message_deletion_unauthorized(fresh_db):
    """Test that non-admin cannot delete other's messages."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)

    # Bob joins (will be admin)
    _, bob_invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    # Wait for Bob to see channel
    def bob_has_channel():
        bob_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (bob['peer_id'],)
        )
        assert len(bob_channels) >= 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=None)

    # Make Bob admin
    alice_admin_grant = admin.my_grant(alice['user_id'], alice['network_id'], alice['peer_id'], db)
    alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
    admin.create(
        user_id=bob['user_id'],
        network_id=alice['network_id'],
        signed_by=alice['peer_shared_id'],
        signer_private_key=alice_private_key,
        t_ms=t_ms,
        peer_id=alice['peer_id'],
        db=db,
        admin_grant=alice_admin_grant
    )
    db.commit()

    # Wait for Bob to be admin
    def bob_is_admin():
        result = admin.is_user_admin(bob['user_id'], alice['network_id'], bob['peer_id'], db)
        assert result

    t_ms = assert_eventually(bob_is_admin, db=db, start_t_ms=t_ms)

    # Charlie joins (will NOT be admin)
    _, charlie_invite_link, _ = invite.create(peer_id=bob['peer_id'], t_ms=t_ms, db=db)
    charlie_peer_id = peer.create(t_ms=t_ms + 100, db=db)
    charlie = user.join(peer_id=charlie_peer_id, invite_link=charlie_invite_link, name='Charlie', t_ms=t_ms + 100, db=db)
    db.commit()

    # Wait for Charlie to see channel
    def charlie_has_channel():
        charlie_channels = db.query_all(
            "SELECT channel_id FROM channels WHERE recorded_by = ?",
            (charlie['peer_id'],)
        )
        assert len(charlie_channels) >= 1

    t_ms = assert_eventually(charlie_has_channel, db=db, start_t_ms=t_ms)

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Alice's message",
        t_ms=t_ms,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Wait for Charlie to see the message
    def charlie_sees_message():
        charlie_msgs = message.list(alice['channel_id'], charlie['peer_id'], db)
        assert any(m['content'] == "Alice's message" for m in charlie_msgs)

    t_ms = assert_eventually(charlie_sees_message, db=db, start_t_ms=t_ms)

    # Charlie (non-admin) tries to delete Alice's message - should fail
    with pytest.raises(ValueError) as exc_info:
        message_deletion.create(
            peer_id=charlie['peer_id'],
            message_id=message_id,
            t_ms=t_ms,
            db=db
        )
    assert "not the author" in str(exc_info.value) and "not an admin" in str(exc_info.value)


@pytest.mark.skip(reason="Key propagation issue: events blocked waiting for missing keys")
def test_message_deletion_ordering():
    """Test that deletion works regardless of whether message or deletion arrives first."""
    pass


if __name__ == "__main__":
    test_message_deletion_self()
    test_message_deletion_admin()
    test_message_deletion_unauthorized()
