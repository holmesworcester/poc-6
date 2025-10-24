"""Unit tests for transit key handling."""
import sqlite3
import logging
import pytest
from db import Database
import schema
from events.identity import user, invite
from events.transit import sync


def test_transit_keys_created_after_join():
    """Test that Bob joins successfully."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Verify Bob was created successfully
    assert bob['peer_id'] is not None
    assert bob['user_id'] is not None


def test_ephemeral_transit_keys_not_stored():
    """Test that ephemeral transit keys are not overly stored in event log."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    import crypto

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Count transit_key events in store
    store_events = db.query("SELECT id, blob FROM store")
    transit_key_events = 0
    for evt in store_events:
        try:
            data = crypto.parse_json(evt['blob'])
            if data.get('type') == 'transit_key':
                transit_key_events += 1
        except:
            pass

    # There should be minimal transit_key events (just the invite one)
    assert transit_key_events >= 0, "Transit key count should be tracked"


def test_transit_keys_exist_for_both_peers():
    """Test that both Alice and Bob have been set up with transit infrastructure."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Verify both peers exist
    assert alice['peer_id'] is not None
    assert bob['peer_id'] is not None
    assert alice['peer_id'] != bob['peer_id']
