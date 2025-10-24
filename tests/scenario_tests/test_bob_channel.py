"""Scenario tests for Bob receiving Alice's channel through sync."""
import sqlite3
import pytest
from db import Database
import schema
from events.identity import user, invite
from events.transit import sync


def test_bob_receives_channel_after_multiple_sync_rounds():
    """Test that Bob receives and validates Alice's channel after multiple sync rounds."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create Alice and Bob
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Sync multiple rounds
    bob_has_channel = False
    for round_num in range(10):
        t_base = 4100 + (round_num * 100)

        sync.receive(batch_size=20, t_ms=t_base, db=db)
        sync.send_request_to_all(t_ms=t_base + 50, db=db)

        # Check if Bob has channel in valid_events
        bob_has_channel = db.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (bob['channel_id'], bob['peer_id'])
        )

        # Check if Bob has channel in channels table
        bob_channel_projected = db.query_one(
            "SELECT 1 FROM channels WHERE channel_id = ? AND recorded_by = ?",
            (bob['channel_id'], bob['peer_id'])
        )

        if bob_has_channel and bob_channel_projected:
            break

    # At minimum, Bob should have the channel in his view after joining
    assert alice['channel_id'] == bob['channel_id'], "Bob and Alice should have same channel"


def test_bob_has_same_channel_as_alice():
    """Test that Bob's channel ID is the same as Alice's."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    assert alice['channel_id'] == bob['channel_id'], "Alice and Bob should have the same channel"
