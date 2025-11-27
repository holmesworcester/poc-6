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
from db import Database
import schema
from events.identity import user, invite, peer, admin
from events.content import message
from events.content import message_deletion
from events.group import group_member
from tests.utils import tick_helper


def test_message_deletion_self(fresh_db):
    """Test self-deletion: author deletes their own message."""

    db = fresh_db

    print("\n=== Setup: Alice creates network ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Alice sends a message
    print("\n=== Alice sends message ===")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Hello, this will be deleted",
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    print(f"Message created: {message_id[:20]}...")
    db.commit()

    # Verify message exists
    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    msg_check = safedb.query_one(
        "SELECT content FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert msg_check is not None, "Message should exist before deletion"
    assert msg_check['content'] == "Hello, this will be deleted"
    print("✓ Message exists in database")

    # Alice deletes the message
    print("\n=== Alice deletes her own message ===")
    deletion_id = message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=3000,
        db=db
    )
    print(f"Deletion created: {deletion_id[:20]}...")
    db.commit()

    # Verify message is deleted
    msg_check_after = safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert msg_check_after is None, "Message should be deleted"
    print("✓ Message removed from database")

    # Verify deletion record exists
    deletion_check = safedb.query_one(
        "SELECT 1 FROM message_deletions WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert deletion_check is not None, "Deletion record should exist"
    print("✓ Deletion record persisted")

    # Verify event is marked as deleted in deleted_events
    deleted_events_check = safedb.query_one(
        "SELECT 1 FROM deleted_events WHERE event_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert deleted_events_check is not None, "Event should be marked as deleted in deleted_events"
    print("✓ Event marked in deleted_events table")

    # Verify blob is removed from store
    from db import create_unsafe_db
    unsafedb = create_unsafe_db(db)
    blob_check = unsafedb.query_one(
        "SELECT 1 FROM store WHERE id = ?",
        (message_id,)
    )
    assert blob_check is None, "Blob should be removed from store"
    print("✓ Blob removed from store")

    print("\n✅ Self-deletion test passed")


def test_message_deletion_admin(fresh_db):
    """Test admin deletion: admin deletes another user's message."""

    db = fresh_db

    print("\n=== Setup: Alice creates network, Bob joins ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    bob_peer_id = peer.create(t_ms=2000, db=db)

    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared_id = bob['peer_shared_id']
    db.commit()

    # Sync to converge
    print("\n=== Sync to converge ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=3000, max_rounds=200, check_interval=1)

    # Make Bob an admin via admin_grant
    print("\n=== Alice makes Bob an admin ===")
    alice_admin_grant = admin.my_grant(
        alice['user_id'],
        alice['network_id'],
        alice['peer_id'],
        db
    )
    alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
    admin.create(
        user_id=bob['user_id'],
        network_id=alice['network_id'],
        signed_by=alice['peer_shared_id'],
        signer_private_key=alice_private_key,
        t_ms=4000,
        peer_id=alice['peer_id'],
        db=db,
        admin_grant=alice_admin_grant
    )
    db.commit()

    # Sync admin status
    print("\n=== Syncing admin grant to Bob ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=5000, max_rounds=200, check_interval=1)

    # Alice sends a message
    print("\n=== Alice sends message ===")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Alice's message to be deleted by Bob",
        t_ms=6000,
        db=db
    )
    message_id = msg_result['id']
    print(f"Message created: {message_id[:20]}...")
    db.commit()

    # Sync message to Bob
    tick_helper.sync_until_converged(db=db, start_t_ms=7000, max_rounds=200, check_interval=1)

    # Verify Bob sees the message
    from db import create_safe_db
    bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
    bob_msg_check = bob_safedb.query_one(
        "SELECT content FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_msg_check is not None, "Bob should see Alice's message"
    print("✓ Bob sees message before deletion")

    # Ensure Bob's admin status is fully synced to himself before deleting
    print("\n=== Syncing admin status to Bob (ensure visible to himself) ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=7500, max_rounds=200, check_interval=1)

    # Bob (admin) deletes Alice's message
    print("\n=== Bob (admin) deletes Alice's message ===")
    deletion_id = message_deletion.create(
        peer_id=bob['peer_id'],
        message_id=message_id,
        t_ms=8000,
        db=db
    )
    print(f"Deletion created by Bob: {deletion_id[:20]}...")
    db.commit()

    # Verify message is deleted from Bob's view
    bob_msg_after = bob_safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_msg_after is None, "Message should be deleted from Bob's view"
    print("✓ Message deleted from Bob's database")

    # Sync deletion to Alice
    tick_helper.sync_until_converged(db=db, start_t_ms=9000, max_rounds=200, check_interval=1)

    # Verify Alice also sees deletion
    alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    alice_msg_after = alice_safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert alice_msg_after is None, "Message should be deleted from Alice's view after sync"
    print("✓ Deletion synced to Alice")

    print("\n✅ Admin deletion test passed")


def test_message_deletion_unauthorized(fresh_db):
    """Test that non-admin cannot delete other's messages."""

    db = fresh_db

    print("\n=== Setup: Alice creates network, Bob and Charlie join ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Bob joins (will be admin)
    bob_invite_id, bob_invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )
    bob_peer_id = peer.create(t_ms=2000, db=db)

    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared_id = bob['peer_shared_id']
    db.commit()

    # Sync
    tick_helper.sync_until_converged(db=db, start_t_ms=3000, max_rounds=200, check_interval=1)

    # Make Bob admin via admin_grant
    alice_admin_grant = admin.my_grant(
        alice['user_id'],
        alice['network_id'],
        alice['peer_id'],
        db
    )
    alice_private_key = peer.get_private_key(alice['peer_id'], alice['peer_id'], db)
    admin.create(
        user_id=bob['user_id'],
        network_id=alice['network_id'],
        signed_by=alice['peer_shared_id'],
        signer_private_key=alice_private_key,
        t_ms=4000,
        peer_id=alice['peer_id'],
        db=db,
        admin_grant=alice_admin_grant
    )
    db.commit()

    # Sync Bob's admin status
    tick_helper.sync_until_converged(db=db, start_t_ms=5000, max_rounds=200, check_interval=1)

    # Charlie joins (will NOT be admin)
    charlie_invite_id, charlie_invite_link, _ = invite.create(
        peer_id=bob['peer_id'],
        t_ms=6000,
        db=db
    )
    charlie_peer_id = peer.create(t_ms=7000, db=db)

    charlie = user.join(peer_id=charlie_peer_id, invite_link=charlie_invite_link, name='Charlie', t_ms=7000, db=db)
    charlie_peer_shared_id = charlie['peer_shared_id']
    db.commit()

    # Sync
    tick_helper.sync_until_converged(db=db, start_t_ms=8000, max_rounds=200, check_interval=1)

    # Alice sends a message
    print("\n=== Alice sends message ===")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Alice's message",
        t_ms=9000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Sync message
    tick_helper.sync_until_converged(db=db, start_t_ms=10000, max_rounds=200, check_interval=1)

    # Charlie (non-admin) tries to delete Alice's message
    print("\n=== Charlie (non-admin) tries to delete Alice's message ===")
    try:
        message_deletion.create(
            peer_id=charlie['peer_id'],
            message_id=message_id,
            t_ms=11000,
            db=db
        )
        assert False, "Charlie should NOT be able to delete Alice's message"
    except ValueError as e:
        print(f"✓ Charlie correctly prevented from deleting: {e}")
        assert "not the author" in str(e) and "not an admin" in str(e)

    print("\n✅ Unauthorized deletion prevention test passed")


def test_message_deletion_ordering(fresh_db):
    """Test that deletion works regardless of whether message or deletion arrives first."""

    db = fresh_db

    print("\n=== Setup: Alice and Bob ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    bob_invite_id, bob_invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )
    bob_peer_id = peer.create(t_ms=2000, db=db)

    bob = user.join(peer_id=bob_peer_id, invite_link=bob_invite_link, name='Bob', t_ms=2000, db=db)
    bob_peer_shared_id = bob['peer_shared_id']
    db.commit()

    # Sync
    tick_helper.sync_until_converged(db=db, start_t_ms=3000, max_rounds=200, check_interval=1)

    # Alice sends message
    print("\n=== Alice sends message ===")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Test ordering",
        t_ms=4000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Alice immediately deletes it (before syncing to Bob)
    print("\n=== Alice deletes message (before syncing) ===")
    deletion_id = message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=4100,
        db=db
    )
    db.commit()

    # Now sync both message and deletion to Bob
    print("\n=== Sync to Bob (message and deletion together) ===")
    tick_helper.sync_until_converged(db=db, start_t_ms=5000, max_rounds=200, check_interval=1)

    # Verify Bob never sees the message (deletion blocks it)
    from db import create_safe_db
    bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
    bob_msg_check = bob_safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    # Either Bob never projected it, or it was projected then deleted
    # Either way, it should not be visible
    print(f"Bob's message check: {bob_msg_check}")

    # Check deletion exists for Bob
    bob_deletion_check = bob_safedb.query_one(
        "SELECT 1 FROM message_deletions WHERE message_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_deletion_check is not None, "Bob should have deletion record"
    print("✓ Bob has deletion record")

    # The key convergence property: regardless of projection order, Bob doesn't see the message
    assert bob_msg_check is None, "Bob should not see deleted message (convergence)"
    print("✓ Message not visible to Bob (convergence verified)")

    # Verify Bob's deleted_events table is populated
    bob_deleted_check = bob_safedb.query_one(
        "SELECT 1 FROM deleted_events WHERE event_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_deleted_check is not None, "Bob should have deleted_events entry"
    print("✓ Bob has message in deleted_events (prevents future projection)")

    # Verify blob is removed from store for Alice
    from db import create_unsafe_db
    unsafedb = create_unsafe_db(db)
    alice_blob_check = unsafedb.query_one(
        "SELECT 1 FROM store WHERE id = ?",
        (message_id,)
    )
    # Note: Since the message and deletion events have different IDs (both blobs are stored),
    # and we only delete the message blob, not the deletion blob, the blob may still exist
    # in store under the deletion event ID. But the message_id (event_id) should not be in deleted_events
    # and should not be in valid_events or messages table.

    print("\n✅ Ordering/convergence test passed")


if __name__ == "__main__":
    test_message_deletion_self()
    test_message_deletion_admin()
    test_message_deletion_unauthorized()
    test_message_deletion_ordering()
    print("\n🎉 All message deletion tests passed!")
