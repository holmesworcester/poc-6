"""Tests for message update (editing) functionality."""
import pytest
import sqlite3
from db import Database
import schema
from events.identity import user, invite, peer
from events.content import message, message_update
from tests.utils import tick_helper


def test_edit_own_message():
    """User can edit their own message."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create network and user
    result = user.new_network(name="alice", t_ms=1000, db=db)
    peer_id = result['peer_id']
    channel_id = result['channel_id']
    db.commit()

    # Send a message
    msg_result = message.create(
        peer_id=peer_id,
        channel_id=channel_id,
        content="original message",
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Verify original content
    messages = message.list(channel_id, peer_id, db)
    assert len(messages) == 1
    assert messages[0]['content'] == "original message"
    assert messages[0]['edited_at'] == 0

    # Edit the message
    update_id = message_update.create(
        message_id=message_id,
        new_content="edited message",
        peer_id=peer_id,
        t_ms=3000,
        db=db
    )
    db.commit()

    # Verify edited content
    messages = message.list(channel_id, peer_id, db)
    assert len(messages) == 1
    assert messages[0]['content'] == "edited message"
    assert messages[0]['edited_at'] > 0


def test_cannot_edit_others_message():
    """User cannot edit another user's message."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create network with alice
    alice_result = user.new_network(name="alice", t_ms=1000, db=db)
    alice_peer_id = alice_result['peer_id']
    channel_id = alice_result['channel_id']
    db.commit()

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice_peer_id,
        channel_id=channel_id,
        content="alice's message",
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Create invite and add bob
    invite_id, invite_link, _ = invite.create(peer_id=alice_peer_id, t_ms=3000, db=db)
    db.commit()

    bob_peer_id = peer.create(t_ms=4000, db=db)
    bob_result = user.join(
        peer_id=bob_peer_id,
        invite_link=invite_link,
        name="bob",
        t_ms=4000,
        db=db
    )
    db.commit()

    # Sync so bob can see the message
    tick_helper.sync_until_converged(db=db, start_t_ms=5000, max_rounds=200, check_interval=1)

    # Bob tries to edit alice's message - should fail
    with pytest.raises(ValueError, match="Only the message author"):
        message_update.create(
            message_id=message_id,
            new_content="bob's edit",
            peer_id=bob_peer_id,
            t_ms=10000,
            db=db
        )


def test_multiple_edits_convergence():
    """Multiple edits converge to highest global_count."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create network and user
    result = user.new_network(name="alice", t_ms=1000, db=db)
    peer_id = result['peer_id']
    channel_id = result['channel_id']
    db.commit()

    # Send a message
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
    message_update.create(
        message_id=message_id,
        new_content="edit 1",
        peer_id=peer_id,
        t_ms=3000,
        db=db
    )
    db.commit()

    message_update.create(
        message_id=message_id,
        new_content="edit 2",
        peer_id=peer_id,
        t_ms=4000,
        db=db
    )
    db.commit()

    message_update.create(
        message_id=message_id,
        new_content="edit 3",
        peer_id=peer_id,
        t_ms=5000,
        db=db
    )
    db.commit()

    # Verify final content is the last edit
    messages = message.list(channel_id, peer_id, db)
    assert messages[0]['content'] == "edit 3"


def test_edit_history():
    """Edit history is tracked correctly."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create network and user
    result = user.new_network(name="alice", t_ms=1000, db=db)
    peer_id = result['peer_id']
    channel_id = result['channel_id']
    db.commit()

    # Send a message
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
    message_update.create(
        message_id=message_id,
        new_content="edit 1",
        peer_id=peer_id,
        t_ms=3000,
        db=db
    )
    db.commit()

    message_update.create(
        message_id=message_id,
        new_content="edit 2",
        peer_id=peer_id,
        t_ms=4000,
        db=db
    )
    db.commit()

    # Get history
    history = message_update.list_history(message_id, peer_id, db)
    assert len(history) == 2
    assert history[0]['new_content'] == "edit 1"
    assert history[0]['global_count'] == 1
    assert history[1]['new_content'] == "edit 2"
    assert history[1]['global_count'] == 2


def test_edit_syncs_to_other_peer():
    """Edits sync to other peers."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create network with alice
    alice_result = user.new_network(name="alice", t_ms=1000, db=db)
    alice_peer_id = alice_result['peer_id']
    channel_id = alice_result['channel_id']
    db.commit()

    # Create invite and add bob
    invite_id, invite_link, _ = invite.create(peer_id=alice_peer_id, t_ms=2000, db=db)
    db.commit()

    bob_peer_id = peer.create(t_ms=3000, db=db)
    bob_result = user.join(
        peer_id=bob_peer_id,
        invite_link=invite_link,
        name="bob",
        t_ms=3000,
        db=db
    )
    db.commit()

    # Sync initial state
    tick_helper.sync_until_converged(db=db, start_t_ms=4000, max_rounds=200, check_interval=1)

    # Alice sends a message
    msg_result = message.create(
        peer_id=alice_peer_id,
        channel_id=channel_id,
        content="original",
        t_ms=10000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Sync so bob can see the message
    tick_helper.sync_until_converged(db=db, start_t_ms=11000, max_rounds=200, check_interval=1)

    # Verify bob sees original
    bob_messages = message.list(channel_id, bob_peer_id, db)
    assert len(bob_messages) == 1
    assert bob_messages[0]['content'] == "original"

    # Alice edits the message
    message_update.create(
        message_id=message_id,
        new_content="edited by alice",
        peer_id=alice_peer_id,
        t_ms=20000,
        db=db
    )
    db.commit()

    # Sync so bob sees the edit
    tick_helper.sync_until_converged(db=db, start_t_ms=21000, max_rounds=200, check_interval=1)

    # Verify bob sees the edit
    bob_messages = message.list(channel_id, bob_peer_id, db)
    assert len(bob_messages) == 1
    assert bob_messages[0]['content'] == "edited by alice"
    assert bob_messages[0]['edited_at'] > 0


def test_empty_content_rejected():
    """Empty content is rejected."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create network and user
    result = user.new_network(name="alice", t_ms=1000, db=db)
    peer_id = result['peer_id']
    channel_id = result['channel_id']
    db.commit()

    # Send a message
    msg_result = message.create(
        peer_id=peer_id,
        channel_id=channel_id,
        content="original",
        t_ms=2000,
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Try to edit with empty content
    with pytest.raises(ValueError, match="cannot be empty"):
        message_update.create(
            message_id=message_id,
            new_content="   ",
            peer_id=peer_id,
            t_ms=3000,
            db=db
        )
