"""
Scenario tests: Unauthorized actions must not be applied via sync.

These tests are expected to fail until projection enforces auth checks
for sensitive events received via sync.
"""
from core.db import create_safe_db, create_unsafe_db
from core import store, recorded
from events.identity import user, invite, peer
from events.content import message_deletion as message_deletion_module
from events.content import message_reaction_deletion as message_reaction_deletion_module
from events.identity import user_removed as user_removed_module
from events.identity import peer_removed as peer_removed_module
from events.content import message, message_reaction
from events.group import group as group_module
from tests.utils.tick_helper import initial_sync, assert_eventually


def _setup_alice_bob(db):
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice["peer_id"], t_ms=1500, db=db)

    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name="Bob", t_ms=2000, db=db)
    db.commit()

    t_ms = initial_sync(db, start_t_ms=None)

    def alice_has_bob_peer_shared():
        safedb = create_safe_db(db, recorded_by=alice["peer_id"])
        row = safedb.query_one(
            "SELECT 1 FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
            (bob["peer_shared_id"], alice["peer_id"]),
        )
        assert row is not None

    t_ms = assert_eventually(alice_has_bob_peer_shared, db=db, start_t_ms=t_ms)
    return alice, bob, t_ms


def test_non_admin_cannot_delete_message_via_sync(fresh_db):
    db = fresh_db

    alice, bob, t_ms = _setup_alice_bob(db)

    # Alice sends a message
    t_ms += 100
    msg = message.create(
        peer_id=alice["peer_id"],
        channel_id=alice["channel_id"],
        content="do not delete me",
        t_ms=t_ms,
        db=db,
    )
    db.commit()

    # Resolve group_id for the message channel
    safedb_alice = create_safe_db(db, recorded_by=alice["peer_id"])
    channel_row = safedb_alice.query_one(
        "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (alice["channel_id"], alice["peer_id"]),
    )
    assert channel_row is not None
    group_id = channel_row["group_id"]

    # Ensure Bob has group key for encryption
    def bob_has_group_key():
        group_module.pick_key(group_id, bob["peer_id"], db)

    t_ms = assert_eventually(bob_has_group_key, db=db, start_t_ms=t_ms)

    # Bob crafts an unauthorized deletion event (not author, not admin)
    bob_private_key = peer.get_private_key(bob["peer_id"], bob["peer_id"], db)
    key_data = group_module.pick_key(group_id, bob["peer_id"], db)
    deletion_blob = message_deletion_module.encode_wire_event(
        message_id_b64=msg["id"],
        signed_by_b64=bob["peer_shared_id"],
        signer_type="peer_shared",
        created_at_ms=t_ms + 1,
        key_data=key_data,
        private_key=bob_private_key,
    )

    # Store the deletion blob and simulate it arriving to Alice via sync
    unsafedb = create_unsafe_db(db)
    deletion_id = store.blob(deletion_blob, t_ms + 1, return_dupes=True, unsafedb=unsafedb)
    recorded_id = recorded.create(deletion_id, alice["peer_id"], t_ms + 2, db, return_dupes=False)
    recorded.project(recorded_id, db)
    db.commit()

    # Expectation: Alice's message should still exist (auth should block deletion).
    # Current behavior deletes it, so this test should fail until auth is enforced.
    alice_msg = safedb_alice.query_one(
        "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
        (msg["id"], alice["peer_id"]),
    )
    assert alice_msg is not None, "Unauthorized deletion should not remove the message"


def test_non_reactor_cannot_delete_reaction_via_sync(fresh_db):
    db = fresh_db
    alice, bob, t_ms = _setup_alice_bob(db)

    # Alice sends a message
    t_ms += 100
    msg = message.create(
        peer_id=alice["peer_id"],
        channel_id=alice["channel_id"],
        content="reaction target",
        t_ms=t_ms,
        db=db,
    )
    db.commit()

    # Alice reacts to her message
    t_ms += 50
    reaction_id = message_reaction.create(
        peer_id=alice["peer_id"],
        message_id=msg["id"],
        emoji="✅",
        t_ms=t_ms,
        db=db,
    )
    db.commit()

    # Resolve group_id for the message channel
    safedb_alice = create_safe_db(db, recorded_by=alice["peer_id"])
    channel_row = safedb_alice.query_one(
        "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (alice["channel_id"], alice["peer_id"]),
    )
    assert channel_row is not None
    group_id = channel_row["group_id"]

    # Ensure Bob has group key for encryption
    def bob_has_group_key():
        group_module.pick_key(group_id, bob["peer_id"], db)

    t_ms = assert_eventually(bob_has_group_key, db=db, start_t_ms=t_ms)

    # Bob crafts an unauthorized reaction deletion (not reactor)
    bob_private_key = peer.get_private_key(bob["peer_id"], bob["peer_id"], db)
    key_data = group_module.pick_key(group_id, bob["peer_id"], db)
    deletion_blob = message_reaction_deletion_module.encode_wire_event(
        reaction_id_b64=reaction_id,
        signed_by_b64=bob["peer_shared_id"],
        signer_type="peer_shared",
        created_at_ms=t_ms + 1,
        key_data=key_data,
        private_key=bob_private_key,
    )

    unsafedb = create_unsafe_db(db)
    deletion_id = store.blob(deletion_blob, t_ms + 1, return_dupes=True, unsafedb=unsafedb)
    recorded_id = recorded.create(deletion_id, alice["peer_id"], t_ms + 2, db, return_dupes=False)
    recorded.project(recorded_id, db)
    db.commit()

    # Reaction should still exist for Alice (auth should block deletion)
    reaction_row = safedb_alice.query_one(
        "SELECT 1 FROM message_reactions WHERE reaction_id = ? AND recorded_by = ?",
        (reaction_id, alice["peer_id"]),
    )
    assert reaction_row is not None, "Unauthorized reaction deletion should not remove reaction"


def test_admin_cannot_delete_reaction_via_sync(fresh_db):
    db = fresh_db
    alice, bob, t_ms = _setup_alice_bob(db)

    # Alice sends a message
    t_ms += 100
    msg = message.create(
        peer_id=alice["peer_id"],
        channel_id=alice["channel_id"],
        content="reaction target 2",
        t_ms=t_ms,
        db=db,
    )
    db.commit()

    # Wait for Bob to receive the message via sync
    def bob_sees_message():
        safedb_bob = create_safe_db(db, recorded_by=bob["peer_id"])
        row = safedb_bob.query_one(
            "SELECT 1 FROM messages WHERE message_id = ? AND recorded_by = ?",
            (msg["id"], bob["peer_id"]),
        )
        assert row is not None

    t_ms = assert_eventually(bob_sees_message, db=db, start_t_ms=t_ms)

    # Bob reacts to Alice's message
    t_ms += 50
    reaction_id = message_reaction.create(
        peer_id=bob["peer_id"],
        message_id=msg["id"],
        emoji="🔥",
        t_ms=t_ms,
        db=db,
    )
    db.commit()

    # Wait for Alice to see Bob's reaction via sync
    safedb_alice = create_safe_db(db, recorded_by=alice["peer_id"])

    def alice_sees_reaction():
        row = safedb_alice.query_one(
            "SELECT 1 FROM message_reactions WHERE reaction_id = ? AND recorded_by = ?",
            (reaction_id, alice["peer_id"]),
        )
        assert row is not None

    t_ms = assert_eventually(alice_sees_reaction, db=db, start_t_ms=t_ms)

    # Resolve group_id for the message channel
    channel_row = safedb_alice.query_one(
        "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (alice["channel_id"], alice["peer_id"]),
    )
    assert channel_row is not None
    group_id = channel_row["group_id"]

    # Ensure Alice has group key for encryption (author/admin)
    def alice_has_group_key():
        group_module.pick_key(group_id, alice["peer_id"], db)

    t_ms = assert_eventually(alice_has_group_key, db=db, start_t_ms=t_ms)

    # Alice (admin) crafts a reaction deletion for Bob's reaction (should be unauthorized)
    alice_private_key = peer.get_private_key(alice["peer_id"], alice["peer_id"], db)
    key_data = group_module.pick_key(group_id, alice["peer_id"], db)
    deletion_blob = message_reaction_deletion_module.encode_wire_event(
        reaction_id_b64=reaction_id,
        signed_by_b64=alice["peer_shared_id"],
        signer_type="peer_shared",
        created_at_ms=t_ms + 1,
        key_data=key_data,
        private_key=alice_private_key,
    )

    unsafedb = create_unsafe_db(db)
    deletion_id = store.blob(deletion_blob, t_ms + 1, return_dupes=True, unsafedb=unsafedb)
    recorded_id = recorded.create(deletion_id, alice["peer_id"], t_ms + 2, db, return_dupes=False)
    recorded.project(recorded_id, db)
    db.commit()

    # Reaction should still exist for Alice (admins cannot delete others' reactions)
    reaction_row = safedb_alice.query_one(
        "SELECT 1 FROM message_reactions WHERE reaction_id = ? AND recorded_by = ?",
        (reaction_id, alice["peer_id"]),
    )
    assert reaction_row is not None, "Admin reaction deletion should not remove reaction"


def test_non_admin_cannot_remove_user_via_sync(fresh_db):
    db = fresh_db
    alice, bob, t_ms = _setup_alice_bob(db)

    # Bob crafts an unauthorized user_removed for Alice
    bob_private_key = peer.get_private_key(bob["peer_id"], bob["peer_id"], db)
    removal_blob = user_removed_module.encode_wire_event(
        removed_user_id_b64=alice["user_id"],
        removed_by_b64=bob["peer_shared_id"],
        signer_type="peer_shared",
        created_at_ms=t_ms + 1,
        private_key=bob_private_key,
    )

    unsafedb = create_unsafe_db(db)
    removal_id = store.blob(removal_blob, t_ms + 1, return_dupes=True, unsafedb=unsafedb)
    recorded_id = recorded.create(removal_id, alice["peer_id"], t_ms + 2, db, return_dupes=False)
    recorded.project(recorded_id, db)
    db.commit()

    safedb_alice = create_safe_db(db, recorded_by=alice["peer_id"])
    removed = safedb_alice.query_one(
        "SELECT 1 FROM removed_users WHERE user_id = ? AND recorded_by = ?",
        (alice["user_id"], alice["peer_id"]),
    )
    assert removed is None, "Unauthorized user_removed should not mark user as removed"


def test_non_admin_cannot_remove_peer_via_sync(fresh_db):
    db = fresh_db
    alice, bob, t_ms = _setup_alice_bob(db)

    # Bob crafts an unauthorized peer_removed for Alice's peer
    bob_private_key = peer.get_private_key(bob["peer_id"], bob["peer_id"], db)
    removal_blob = peer_removed_module.encode_wire_event(
        removed_peer_id_b64=alice["peer_shared_id"],
        removed_by_b64=bob["peer_shared_id"],
        signer_type="peer_shared",
        created_at_ms=t_ms + 1,
        private_key=bob_private_key,
    )

    unsafedb = create_unsafe_db(db)
    removal_id = store.blob(removal_blob, t_ms + 1, return_dupes=True, unsafedb=unsafedb)
    recorded_id = recorded.create(removal_id, alice["peer_id"], t_ms + 2, db, return_dupes=False)
    recorded.project(recorded_id, db)
    db.commit()

    # removed_peers is device-wide (not recorded_by-scoped)
    removed = unsafedb.query_one(
        "SELECT 1 FROM removed_peers WHERE peer_shared_id = ?",
        (alice["peer_shared_id"],),
    )
    assert removed is None, "Unauthorized peer_removed should not mark peer as removed"
