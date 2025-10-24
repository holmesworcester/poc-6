"""Test channel synchronization between peers."""
import sqlite3
import pytest
from db import Database
import schema
from events.identity import user, invite
from events.transit import sync


def test_alice_channel_shared_after_bob_joins():
    """Test that Bob and Alice share the same channel ID after Bob joins."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Alice creates a network
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    assert alice['channel_id'] is not None

    # Create invite
    invite_id, invite_link, invite_data = invite.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    # Bob joins
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    assert bob['peer_id'] != alice['peer_id']

    # They should have the same channel
    assert alice['channel_id'] == bob['channel_id'], "Alice and Bob should have the same channel after Bob joins"


def test_channel_in_shareable_events():
    """Test that channel is in shareable events."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Check Alice's shareable events
    alice_shareable = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    )

    event_ids = [evt['event_id'] for evt in alice_shareable]
    assert alice['channel_id'] in event_ids, "Channel should be in Alice's shareable events"


def test_bob_has_channel_in_shareable_events_after_join():
    """Test that Bob's channel ID is set after joining."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Check Bob's shareable events
    bob_shareable = db.query(
        "SELECT event_id, window_id FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    )

    # They should have the same channel
    assert alice['channel_id'] == bob['channel_id'], "Alice and Bob should have the same channel"
    assert bob['channel_id'] is not None, "Bob should have a channel"


def test_events_in_common_between_peers():
    """Test that Alice and Bob have the same channel."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # They should have the same channel
    assert alice['channel_id'] == bob['channel_id'], "Alice and Bob should share the same channel"
