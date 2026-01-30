"""Tests for message update (editing) functionality."""
import pytest
import sqlite3
from core.db import Database
from core import schema
from events.identity import user, invite, peer
from events.content import message, message_update
from tests.utils.tick_helper import assert_eventually, TestClock


def test_edit_own_message():
    """User can edit their own message."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    result = user.new_network(name="alice", t_ms=1000, db=db)
    peer_id = result['peer_id']
    channel_id = result['channel_id']
    db.commit()

    msg_result = message.create(
        peer_id=peer_id,
        channel_id=channel_id,
        content="original message",
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Verify original
    messages = message.list(channel_id, peer_id, db)
    assert len(messages) == 1
    assert messages[0]['content'] == "original message"
    assert messages[0]['edited_at'] == 0

    # Edit
    message_update.create(
        message_id=message_id,
        new_content="edited message",
        peer_id=peer_id,
        t_ms=3000,
        db=db
    )
    db.commit()

    # Verify edit
    messages = message.list(channel_id, peer_id, db)
    assert len(messages) == 1
    assert messages[0]['content'] == "edited message"
    assert messages[0]['edited_at'] > 0


def test_cannot_edit_others_message(fresh_db):
    """User cannot edit another user's message."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name="alice", t_ms=clock.tick(), db=db)
    channel_id = alice['channel_id']

    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="alice's message",
        t_ms=clock.tick(),
        db=db
    )
    message_id = msg_result['id']

    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name="bob", t_ms=clock.now(), db=db)
    db.commit()

    # Wait for bob to see the message
    def bob_sees_message():
        msgs = message.list(channel_id, bob['peer_id'], db)
        assert any(m['content'] == "alice's message" for m in msgs)

    assert_eventually(bob_sees_message, db=db, start_t_ms=None)

    # Bob tries to edit alice's message - should fail
    with pytest.raises(ValueError, match="Only the message author"):
        message_update.create(
            message_id=message_id,
            new_content="bob's edit",
            peer_id=bob['peer_id'],
            t_ms=10000,
            db=db
        )


def test_multiple_edits_convergence():
    """Multiple edits converge to highest global_count."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    result = user.new_network(name="alice", t_ms=1000, db=db)
    peer_id = result['peer_id']
    channel_id = result['channel_id']
    db.commit()

    msg_result = message.create(
        peer_id=peer_id,
        channel_id=channel_id,
        content="original",
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Edit multiple times
    for i, content in enumerate(["edit 1", "edit 2", "edit 3"], start=1):
        message_update.create(
            message_id=message_id,
            new_content=content,
            peer_id=peer_id,
            t_ms=2000 + i * 1000,
            db=db
        )
        db.commit()

    # Verify final content
    messages = message.list(channel_id, peer_id, db)
    assert messages[0]['content'] == "edit 3"


def test_edit_history():
    """Edit history is tracked correctly."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    result = user.new_network(name="alice", t_ms=1000, db=db)
    peer_id = result['peer_id']
    channel_id = result['channel_id']
    db.commit()

    msg_result = message.create(
        peer_id=peer_id,
        channel_id=channel_id,
        content="original",
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Edit twice
    message_update.create(message_id=message_id, new_content="edit 1", peer_id=peer_id, t_ms=3000, db=db)
    db.commit()
    message_update.create(message_id=message_id, new_content="edit 2", peer_id=peer_id, t_ms=4000, db=db)
    db.commit()

    # Verify history
    history = message_update.list_history(message_id, peer_id, db)
    assert len(history) == 2
    assert history[0]['new_content'] == "edit 1"
    assert history[0]['global_count'] == 1
    assert history[1]['new_content'] == "edit 2"
    assert history[1]['global_count'] == 2


def test_edit_syncs_to_other_peer(fresh_db):
    """Edits sync to other peers."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name="alice", t_ms=clock.tick(), db=db)
    channel_id = alice['channel_id']

    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name="bob", t_ms=clock.now(), db=db)
    db.commit()

    # Wait for bob to join and see channel
    def bob_has_channel():
        channels = db.query_all("SELECT channel_id FROM channels WHERE recorded_by = ?", (bob['peer_id'],))
        assert len(channels) >= 1

    t_ms = assert_eventually(bob_has_channel, db=db, start_t_ms=None)

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=channel_id,
        content="original",
        t_ms=t_ms,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Wait for bob to see original
    def bob_sees_original():
        msgs = message.list(channel_id, bob['peer_id'], db)
        assert any(m['content'] == "original" for m in msgs)

    t_ms = assert_eventually(bob_sees_original, db=db, start_t_ms=t_ms)

    # Alice edits
    message_update.create(
        message_id=message_id,
        new_content="edited by alice",
        peer_id=alice['peer_id'],
        t_ms=t_ms,
        db=db
    )
    db.commit()

    # Wait for bob to see edit
    def bob_sees_edit():
        msgs = message.list(channel_id, bob['peer_id'], db)
        assert any(m['content'] == "edited by alice" and m['edited_at'] > 0 for m in msgs)

    assert_eventually(bob_sees_edit, db=db, start_t_ms=t_ms)


def test_empty_content_rejected():
    """Empty content is rejected."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    result = user.new_network(name="alice", t_ms=1000, db=db)
    peer_id = result['peer_id']
    channel_id = result['channel_id']
    db.commit()

    msg_result = message.create(
        peer_id=peer_id,
        channel_id=channel_id,
        content="original",
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    with pytest.raises(ValueError, match="cannot be empty"):
        message_update.create(
            message_id=message_id,
            new_content="   ",
            peer_id=peer_id,
            t_ms=3000,
            db=db
        )
