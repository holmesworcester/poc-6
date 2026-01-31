"""
Scenario test: Deleted events are truly purged and cannot be re-downloaded.

When an event is deleted:
1. The blob is removed from store
2. It's removed from shareable_events (won't sync to others)
3. It's added to deleted_events (permanent block)
4. If the event arrives again (e.g., via sync), it's immediately purged

This ensures deleted content is truly gone and cannot be recovered via sync.
"""
import pytest
import sqlite3
from core.db import Database, create_safe_db, create_unsafe_db
from core import schema, store, crypto, recorded
from events.identity import user, invite, peer as peer_module
from events.content import message as message_module
from events.content import message, channel
from events.content import message_deletion
from core import tick


def test_deleted_message_blob_is_purged(fresh_db):
    """Verify that when a message is deleted, its blob is removed from store."""
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="This message will be deleted",
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Verify blob exists in store
    unsafedb = create_unsafe_db(db)
    blob_before = unsafedb.query_one("SELECT 1 FROM store WHERE id = ?", (message_id,))
    assert blob_before is not None, "Message blob should exist before deletion"

    # Verify message is in shareable_events
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    shareable_before = safedb.query_one(
        "SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (message_id, alice['peer_id'])
    )
    assert shareable_before is not None, "Message should be shareable before deletion"

    # Alice deletes the message
    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=3000,
        db=db
    )
    db.commit()

    # Verify blob is GONE from store
    blob_after = unsafedb.query_one("SELECT 1 FROM store WHERE id = ?", (message_id,))
    assert blob_after is None, "Message blob should be purged after deletion"

    # Verify message is NOT in shareable_events
    shareable_after = safedb.query_one(
        "SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (message_id, alice['peer_id'])
    )
    assert shareable_after is None, "Message should not be shareable after deletion"

    # Verify message is NOT in negentropy_events
    negentropy_after = safedb.query_one(
        "SELECT 1 FROM negentropy_events WHERE event_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert negentropy_after is None, "Deleted message should be removed from negentropy_events"

    # Verify it's in deleted_events (permanent block)
    deleted_check = safedb.query_one(
        "SELECT 1 FROM deleted_events WHERE event_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert deleted_check is not None, "Message should be in deleted_events"


# TODO: Update to use sender_key model - group.pick_key() was removed
@pytest.mark.skip(reason="Uses removed group.pick_key() API - needs update for sender key model")
def test_deleted_message_arriving_via_sync_is_immediately_purged(fresh_db):
    """
    When a message arrives via sync AFTER a deletion event was already processed,
    the message should be immediately purged and never added to shareable_events.

    This simulates: deletion arrives first, then message arrives via sync later.
    """
    db = fresh_db

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    # First, create a message blob manually (simulating it arriving via sync)
    # We need to create the message event data
    from events.identity import peer_shared
    identity = peer_shared.get_self(alice['peer_id'], db)

    channels = channel.list(alice['peer_id'], db)
    general_channel = next(c for c in channels if c['name'] == 'general')
    channel_id = general_channel['channel_id']
    group_id = general_channel['group_id']

    private_key = peer_module.get_private_key(alice['peer_id'], alice['peer_id'], db)
    from events.group import group
    key_data = group.pick_key(group_id, alice['peer_id'], db)
    blob = message_module.encode_wire_event(
        channel_id_b64=channel_id,
        author_id_b64=alice['user_id'],
        signed_by_b64=identity['peer_shared_id'],
        signer_type="peer_shared",
        content="Message that will arrive after deletion",
        created_at_ms=2000,
        ttl_ms=0,
        key_data=key_data,
        private_key=private_key,
    )

    unsafedb = create_unsafe_db(db)
    message_id = store.blob(blob, 2000, return_dupes=True, unsafedb=unsafedb)
    db.commit()

    # Now create and process a deletion for this message BEFORE the message is projected
    # This simulates deletion arriving first via sync
    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    safedb.execute(
        "INSERT INTO deleted_events (event_id, recorded_by, deleted_at) VALUES (?, ?, ?)",
        (message_id, alice['peer_id'], 2500)
    )
    db.commit()

    # Verify message blob exists but is not yet projected
    blob_exists = unsafedb.query_one("SELECT 1 FROM store WHERE id = ?", (message_id,))
    assert blob_exists is not None, "Message blob should exist before projection attempt"

    msg_projected = safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert msg_projected is None, "Message should not be projected yet"

    # Now simulate the message "arriving" via sync by creating a recorded event and projecting
    recorded_id = recorded.create(message_id, alice['peer_id'], 3000, db, return_dupes=False)
    recorded.project(recorded_id, db)
    db.commit()

    # The message should NOT be projected (skip_if_deleted)
    msg_after = safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert msg_after is None, "Message should NOT be projected because it's deleted"

    # The blob should be PURGED
    blob_after = unsafedb.query_one("SELECT 1 FROM store WHERE id = ?", (message_id,))
    assert blob_after is None, "Message blob should be purged when deleted message arrives"

    # The message should NOT be in shareable_events
    shareable = safedb.query_one(
        "SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (message_id, alice['peer_id'])
    )
    assert shareable is None, "Deleted message should NEVER be added to shareable_events"

    # The message should NOT be in negentropy_events
    negentropy = safedb.query_one(
        "SELECT 1 FROM negentropy_events WHERE event_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert negentropy is None, "Deleted message should NEVER be added to negentropy_events"


def test_multi_peer_deleted_message_not_resynced(fresh_db):
    """
    In a multi-peer scenario:
    1. Alice sends a message
    2. Bob receives the message
    3. Alice deletes the message
    4. Bob processes the deletion
    5. Verify Bob's copy is also purged and won't resync
    """
    db = fresh_db

    # Setup Alice and Bob
    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    invite_id, invite_link, _ = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    bob_peer_id = peer_module.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Tick to sync group keys
    for i in range(15):
        tick.tick(t_ms=2100 + i * 200, db=db)
    db.commit()

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Message to be deleted across peers",
        t_ms=5000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # SAVE the original blob before it gets deleted (for resync simulation later)
    unsafedb = create_unsafe_db(db)
    original_blob_row = unsafedb.query_one("SELECT blob FROM store WHERE id = ?", (message_id,))
    assert original_blob_row is not None, "Message blob should exist"
    original_blob = original_blob_row['blob']

    # Propagate message to Bob
    bob_recorded_id = recorded.create(message_id, bob['peer_id'], 5500, db, return_dupes=False)
    recorded.project(bob_recorded_id, db)
    db.commit()

    # Verify Bob has the message
    bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
    bob_msg = bob_safedb.query_one(
        "SELECT content FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_msg is not None, "Bob should have the message"
    assert bob_msg['content'] == "Message to be deleted across peers"

    # Alice deletes the message
    deletion_id = message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=6000,
        db=db
    )
    db.commit()

    # Verify Alice's copy is purged
    alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])

    alice_msg = alice_safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    assert alice_msg is None, "Alice's message should be deleted"

    alice_shareable = alice_safedb.query_one(
        "SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (message_id, alice['peer_id'])
    )
    assert alice_shareable is None, "Message should not be shareable by Alice"

    # Propagate deletion to Bob
    bob_deletion_recorded_id = recorded.create(deletion_id, bob['peer_id'], 6500, db, return_dupes=False)
    recorded.project(bob_deletion_recorded_id, db)
    db.commit()

    # Verify Bob's copy is also purged
    bob_msg_after = bob_safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_msg_after is None, "Bob's message should be deleted"

    bob_shareable = bob_safedb.query_one(
        "SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_shareable is None, "Message should not be shareable by Bob"

    bob_negentropy = bob_safedb.query_one(
        "SELECT 1 FROM negentropy_events WHERE event_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_negentropy is None, "Message should not be in negentropy_events for Bob"

    bob_deleted = bob_safedb.query_one(
        "SELECT 1 FROM deleted_events WHERE event_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_deleted is not None, "Message should be in Bob's deleted_events"

    # Now simulate the message trying to "come back" via sync to Bob
    # (e.g., from a third peer who hasn't seen the deletion yet)
    # Re-insert the ORIGINAL blob to simulate it arriving again via sync
    unsafedb.execute(
        "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
        (message_id, original_blob, 7000)
    )
    db.commit()

    # Verify blob is back in store
    blob_reinserted = unsafedb.query_one("SELECT 1 FROM store WHERE id = ?", (message_id,))
    assert blob_reinserted is not None, "Blob should be re-inserted for resync test"

    # Try to project it for Bob (simulating sync)
    # Use return_dupes=True since the recorded event already exists from first sync
    resync_recorded_id = recorded.create(message_id, bob['peer_id'], 7500, db, return_dupes=True)
    recorded.project(resync_recorded_id, db)
    db.commit()

    # The message should STILL not be projected (blocked by deleted_events)
    bob_msg_resync = bob_safedb.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_msg_resync is None, "Re-synced deleted message should NOT be projected"

    # The blob should be purged again
    blob_after_resync = unsafedb.query_one("SELECT 1 FROM store WHERE id = ?", (message_id,))
    assert blob_after_resync is None, "Re-synced deleted message blob should be purged"

    # Still not in shareable_events
    bob_shareable_resync = bob_safedb.query_one(
        "SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_shareable_resync is None, "Re-synced deleted message should not be shareable"

    bob_negentropy_resync = bob_safedb.query_one(
        "SELECT 1 FROM negentropy_events WHERE event_id = ? AND recorded_by = ?",
        (message_id, bob['peer_id'])
    )
    assert bob_negentropy_resync is None, "Re-synced deleted message should not re-enter negentropy_events"


if __name__ == "__main__":
    import sqlite3
    from core.db import Database
    from core import schema

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    test_deleted_message_blob_is_purged(db)
    print("test_deleted_message_blob_is_purged passed")

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    test_deleted_message_arriving_via_sync_is_immediately_purged(db)
    print("test_deleted_message_arriving_via_sync_is_immediately_purged passed")

    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    test_multi_peer_deleted_message_not_resynced(db)
    print("test_multi_peer_deleted_message_not_resynced passed")
