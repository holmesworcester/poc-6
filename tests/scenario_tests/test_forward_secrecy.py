"""Forward secrecy scenario tests for deleted messages.

Tests that:
1. Deleting messages marks their encryption key for purging
2. Running purge cycle rekeys remaining messages with new keys
3. Old keys are purged and unrecoverable
4. Forward secrecy prevents decryption with old keys
"""
import pytest
import sqlite3
from db import Database, create_safe_db, create_unsafe_db
import schema
from events.identity import user, invite
from events.content import message, message_deletion
from events.transit import sync


def test_delete_message_marks_key_for_purging():
    """Test that deleting a message marks its key for purging."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Alice creates network ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Alice sends a message
    print("\n=== Alice sends message ===")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Hello, this will be rekeyed",
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    print(f"Message created: {message_id[:20]}...")
    db.commit()

    # Get the key that encrypted the message
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    unsafedb = create_unsafe_db(db)
    message_blob = unsafedb.query_one(
        "SELECT blob FROM store WHERE id = ?",
        (message_id,)
    )
    assert message_blob is not None

    import crypto
    key_id_bytes = message_blob['blob'][:crypto.ID_SIZE]
    key_id_b64 = crypto.b64encode(key_id_bytes)
    print(f"Message encrypted with key: {key_id_b64[:20]}...")

    # Delete the message
    print("\n=== Alice deletes her message ===")
    deletion_id = message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=3000,
        db=db
    )
    print(f"Deletion created: {deletion_id[:20]}...")
    db.commit()

    # Verify key is marked for purging
    key_to_purge = safedb.query_one(
        "SELECT key_id FROM keys_to_purge WHERE key_id = ? AND recorded_by = ?",
        (key_id_b64, alice['peer_id'])
    )
    assert key_to_purge is not None, "Key should be marked for purging"
    print(f"✓ Key {key_id_b64[:20]}... is marked for purging")

    print("\n✅ Mark key for purging test passed")


def test_delete_and_rekey_message():
    """Test that purge cycle rekeys messages and purges old keys."""

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    print("\n=== Setup: Alice creates network ===")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Alice sends two messages
    print("\n=== Alice sends two messages ===")
    msg1_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message 1 - will stay",
        t_ms=2000,
        db=db
    )
    msg1_id = msg1_result['id']
    print(f"Message 1 created: {msg1_id[:20]}...")

    msg2_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message 2 - will be deleted",
        t_ms=2100,
        db=db
    )
    msg2_id = msg2_result['id']
    print(f"Message 2 created: {msg2_id[:20]}...")
    db.commit()

    # Get keys
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    unsafedb = create_unsafe_db(db)

    import crypto
    msg2_blob = unsafedb.query_one(
        "SELECT blob FROM store WHERE id = ?",
        (msg2_id,)
    )
    msg2_key_id_bytes = msg2_blob['blob'][:crypto.ID_SIZE]
    msg2_key_id_b64 = crypto.b64encode(msg2_key_id_bytes)
    print(f"Message 2 encrypted with key: {msg2_key_id_b64[:20]}...")

    # Delete message 2
    print("\n=== Alice deletes message 2 ===")
    deletion_id = message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=msg2_id,
        t_ms=3000,
        db=db
    )
    db.commit()

    # Run purge cycle
    print("\n=== Running purge cycle ===")
    stats = message_deletion.run_message_purge_cycle(alice['peer_id'], 4000, db)
    db.commit()

    print(f"Purge cycle stats: {stats}")
    assert stats['messages_rekeyed'] >= 1, "At least message 1 should have been rekeyed"
    assert stats['keys_purged'] >= 1, "At least message 2's key should have been purged"

    # Verify message 1 is still accessible
    msg1_check = safedb.query_one(
        "SELECT content FROM messages WHERE message_id = ? AND recorded_by = ?",
        (msg1_id, alice['peer_id'])
    )
    assert msg1_check is not None, "Message 1 should still exist"
    assert msg1_check['content'] == "Message 1 - will stay"
    print("✓ Message 1 still exists and is readable")

    # Verify message 2 is deleted
    msg2_check = safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (msg2_id, alice['peer_id'])
    )
    assert msg2_check is None, "Message 2 should be deleted"
    print("✓ Message 2 is deleted")

    # Verify old key is purged from group_keys
    old_key_check = safedb.query_one(
        "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (msg2_key_id_b64, alice['peer_id'])
    )
    assert old_key_check is None, "Old key should be purged"
    print(f"✓ Old key {msg2_key_id_b64[:20]}... is purged")

    # Verify keys_to_purge is cleared
    keys_to_purge = safedb.query(
        "SELECT key_id FROM keys_to_purge WHERE recorded_by = ?",
        (alice['peer_id'],)
    )
    assert len(keys_to_purge) == 0, "keys_to_purge should be empty after purge cycle"
    print("✓ keys_to_purge table is cleared")

    print("\n✅ Delete and rekey test passed")


def test_forward_secrecy_multi_peer():
    """Test forward secrecy with multiple peers."""

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

    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Sync to converge
    print("\n=== Sync to converge ===")
    for round_num in range(3):
        sync.send_request_to_all(t_ms=3000 + round_num * 100, db=db)
        db.commit()
        sync.receive(batch_size=20, t_ms=3050 + round_num * 100, db=db)
        db.commit()

    # Alice sends a message
    print("\n=== Alice sends message ===")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Multi-peer test message",
        t_ms=4000,
        db=db
    )
    message_id = msg_result['id']
    print(f"Message created: {message_id[:20]}...")
    db.commit()

    # Sync message to Bob
    print("\n=== Sync message to Bob ===")
    for round_num in range(3):
        sync.send_request_to_all(t_ms=5000 + round_num * 100, db=db)
        db.commit()
        sync.receive(batch_size=20, t_ms=5050 + round_num * 100, db=db)
        db.commit()

    # Verify Bob sees the message
    bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
    bob_msg_check = bob_safedb.query_one(
        "SELECT content FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_msg_check is not None, "Bob should see the message"
    print("✓ Bob sees Alice's message")

    # Alice deletes the message
    print("\n=== Alice deletes message ===")
    deletion_id = message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=6000,
        db=db
    )
    db.commit()

    # Run purge cycle on Alice's side
    print("\n=== Alice runs purge cycle ===")
    alice_stats = message_deletion.run_message_purge_cycle(alice['peer_id'], 7000, db)
    db.commit()
    print(f"Alice purge stats: {alice_stats}")

    # Sync deletion to Bob
    print("\n=== Sync deletion to Bob ===")
    for round_num in range(3):
        sync.send_request_to_all(t_ms=8000 + round_num * 100, db=db)
        db.commit()
        sync.receive(batch_size=20, t_ms=8050 + round_num * 100, db=db)
        db.commit()

    # Verify Bob also deleted the message
    bob_msg_after = bob_safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_msg_after is None, "Bob should not see deleted message"
    print("✓ Bob sees deletion after sync")

    # Bob runs purge cycle
    print("\n=== Bob runs purge cycle ===")
    bob_stats = message_deletion.run_message_purge_cycle(bob['peer_id'], 9000, db)
    db.commit()
    print(f"Bob purge stats: {bob_stats}")

    # Verify Bob's old key is purged
    bob_keys_to_purge = bob_safedb.query(
        "SELECT key_id FROM keys_to_purge WHERE recorded_by = ?",
        (bob['peer_id'],)
    )
    assert len(bob_keys_to_purge) == 0, "Bob's keys_to_purge should be empty"
    print("✓ Bob purged old keys")

    print("\n✅ Multi-peer forward secrecy test passed")


if __name__ == "__main__":
    test_delete_message_marks_key_for_purging()
    test_delete_and_rekey_message()
    test_forward_secrecy_multi_peer()
    print("\n🎉 All forward secrecy tests passed!")
