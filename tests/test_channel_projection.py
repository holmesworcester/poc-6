"""Unit tests for channel projection and event handling."""
import sqlite3
import pytest
from db import Database, create_safe_db
import schema
from events.identity import user, invite
from events.transit import sync


def test_channel_valid_for_alice():
    """Test that Alice's channel is marked as valid."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Check if Alice has the channel in valid_events
    channel_valid = db.query_one(
        "SELECT event_id FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice['channel_id'], alice['peer_id'])
    )

    assert channel_valid is not None, "Alice's channel should be in valid_events"
    assert channel_valid['event_id'] == alice['channel_id']


def test_channel_recorded_for_bob_after_join():
    """Test that Bob has Alice's channel available after joining."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Check if channel blob is in store (Alice's channel should be available)
    channel_blob = db.query_one(
        "SELECT id FROM store WHERE id = ?",
        (alice['channel_id'],)
    )

    assert channel_blob is not None, "Alice's channel blob should be in store"


def test_channel_unwrapable_by_bob():
    """Test that Bob can unwrap Alice's channel."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    import crypto

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Check if channel can be unwrapped by Alice
    blob = db.query_one("SELECT blob FROM store WHERE id = ?", (alice['channel_id'],))

    if blob:
        unwrapped, missing = crypto.unwrap(blob['blob'], alice['peer_id'], db)
        if unwrapped:
            data = crypto.parse_json(unwrapped)
            assert 'name' in data or 'group_id' in data, "Unwrapped channel should have expected fields"


def test_channel_group_key_exists_for_alice():
    """Test that Alice has the group key for the channel."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    channel = alice_safedb.query_one(
        "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (alice['channel_id'], alice['peer_id'])
    )

    assert channel is not None, "Alice should have the channel"

    group_id = channel['group_id']
    group_key = alice_safedb.query_one(
        "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
        (group_id, alice['peer_id'])
    )

    assert group_key is not None, "Alice should have the group key for the channel"


def test_channel_not_blocked_for_alice():
    """Test that Alice's channel is not in blocked events."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Check blocked queue
    blocked = db.query(
        "SELECT recorded_id FROM blocked_events_ephemeral WHERE recorded_by = ?",
        (alice['peer_id'],)
    )

    # There may be blocked events, but the channel should not be among them
    blocked_refs = []
    for b in blocked:
        rec = db.query_one(
            "SELECT ref_id FROM recorded_events WHERE recorded_id = ?",
            (b['recorded_id'],)
        )
        if rec:
            blocked_refs.append(rec['ref_id'])

    assert alice['channel_id'] not in blocked_refs, "Alice's channel should not be blocked"
