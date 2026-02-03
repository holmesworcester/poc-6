"""Forward secrecy CLI tests for sender key model.

Tests CLI commands related to forward secrecy:
- purge-keys command
- delete command (marks keys for purging)

Note: The 'keys' CLI command needs updating to display sender keys
instead of the removed group_key/group_prekey tables.
"""
import pytest
import sqlite3
from core.db import Database, create_safe_db
from core import schema
from events.identity import user, peer, invite
from events.content import message, message_deletion
from tests.utils.tick_helper import TestClock


@pytest.fixture
def fresh_db():
    """Create fresh in-memory database for each test."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)
    return db


def test_delete_message_marks_key_for_purging(fresh_db):
    """Deleting a message marks its key for purging (API equivalent of CLI delete)."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Send a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Test message",
        t_ms=clock.tick(),
        db=db
    )
    message_id = msg_result['id']
    db.commit()

    # Get the key_id
    alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    msg_row = alice_safedb.query_one(
        "SELECT key_id FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, alice['peer_id'])
    )
    key_id = msg_row['key_id']

    # Delete the message (simulates CLI 'delete' command)
    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=message_id,
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Key should be marked for purging
    purge_entry = alice_safedb.query_one(
        "SELECT 1 FROM keys_to_purge WHERE key_id = ? AND recorded_by = ?",
        (key_id, alice['peer_id'])
    )
    assert purge_entry is not None, "Delete should mark key for purging"


def test_purge_keys_command(fresh_db):
    """purge-keys command runs the purge cycle (API equivalent)."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Send and delete a message to create purge work
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="To be deleted",
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Run purge cycle (simulates CLI 'purge-keys' command)
    stats = message_deletion.run_message_purge_cycle(
        peer_id=alice['peer_id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Should have purged at least one key
    assert stats['keys_purged'] >= 1, "purge-keys should purge orphaned keys"


def test_automatic_purge_on_tick(fresh_db):
    """Purge runs for all peers (simulates background job)."""
    db = fresh_db
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    db.commit()

    # Send and delete a message
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content="Ephemeral",
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    message_deletion.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Run purge for all peers (simulates automatic purge job)
    stats = message_deletion.run_message_purge_cycle_for_all_peers(
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    # Should have processed at least one peer
    assert stats['peers_processed'] >= 1
    assert len(stats['errors']) == 0


@pytest.mark.skip(reason="CLI 'keys' command needs updating for sender key model")
def test_keys_display():
    """The 'keys' CLI command should display sender keys.

    TODO: Update cmd_keys() in cli.py to display:
    - secrets (sender keys)
    - pubkeys (for key distribution)
    - secrets_shared (key distribution records)
    - keys_to_purge (pending purge queue)

    The old group_key and group_prekey tables were removed.
    """
    pass


if __name__ == "__main__":
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    test_delete_message_marks_key_for_purging(db)
    print("test_delete_message_marks_key_for_purging passed")
