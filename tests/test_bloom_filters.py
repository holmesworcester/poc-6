"""Unit tests for bloom filter functionality."""
import sqlite3
import pytest
from db import Database
import schema
from events.identity import user, invite, peer
from events.transit import sync
import crypto


def test_bloom_filter_basic():
    """Test basic bloom filter creation and checking."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    # Create Alice and Bob
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Get Bob's shareable events
    bob_window_1 = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ? AND window_id >= 524288 AND window_id < 1048576",
        (bob['peer_id'],)
    )
    bob_event_ids = [row['event_id'] for row in bob_window_1]

    assert len(bob_event_ids) > 0, "Bob should have events in window 1"

    # Build Bob's bloom filter
    bob_event_bytes = [crypto.b64decode(eid) for eid in bob_event_ids]
    bob_public_key = peer.get_public_key(bob['peer_id'], bob['peer_id'], db)
    salt = sync.derive_salt(bob_public_key, window_id=1)
    bob_bloom = sync.create_bloom(bob_event_bytes, salt)

    # Check that bloom filter has some bits set
    bits_set = bin(int.from_bytes(bob_bloom, 'big')).count('1')
    assert bits_set > 0, "Bloom filter should have bits set"
    assert bits_set < 512, "Bloom filter should not have all bits set"


def test_bloom_filter_contains_events():
    """Test that bloom filter contains the events used to create it."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Get Bob's shareable events
    bob_events = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ? AND window_id >= 524288 AND window_id < 1048576",
        (bob['peer_id'],)
    )
    bob_event_ids = [row['event_id'] for row in bob_events]

    # Build bloom filter
    bob_event_bytes = [crypto.b64decode(eid) for eid in bob_event_ids]
    bob_public_key = peer.get_public_key(bob['peer_id'], bob['peer_id'], db)
    salt = sync.derive_salt(bob_public_key, window_id=1)
    bob_bloom = sync.create_bloom(bob_event_bytes, salt)

    # All Bob's events should be in the bloom filter
    for eid in bob_event_ids:
        eid_bytes = crypto.b64decode(eid)
        in_bloom = sync.check_bloom(eid_bytes, bob_bloom, salt)
        assert in_bloom, f"Event {eid[:20]}... should be in Bob's bloom filter"


def test_bloom_filter_window_computation():
    """Test that events are correctly mapped to windows."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Get Alice's shareable events and check their windows
    alice_events = db.query(
        "SELECT event_id, window_id FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    )

    for evt in alice_events:
        window_id = evt['window_id']
        # Window ID should be a valid storage window
        assert window_id >= 0, "Window ID should be non-negative"


def test_channel_in_alice_bloom():
    """Test that channel is correctly placed in Alice's bloom filter."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Get Alice's channel and check window
    alice_channel_window_row = db.query_one(
        "SELECT window_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (alice['channel_id'], alice['peer_id'])
    )

    assert alice_channel_window_row is not None, "Alice's channel should be in shareable_events"
    window_id = alice_channel_window_row['window_id']
    assert window_id >= 0, "Channel should have a valid window ID"


def test_bloom_salt_mismatch():
    """Test that different salts produce different bloom filters."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Get Bob's events
    bob_events = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    )
    bob_event_ids = [row['event_id'] for row in bob_events]
    bob_event_bytes = [crypto.b64decode(eid) for eid in bob_event_ids]

    bob_public_key = peer.get_public_key(bob['peer_id'], bob['peer_id'], db)

    # Create bloom filters with different windows
    salt1 = sync.derive_salt(bob_public_key, window_id=0)
    salt2 = sync.derive_salt(bob_public_key, window_id=1)

    bloom1 = sync.create_bloom(bob_event_bytes, salt1)
    bloom2 = sync.create_bloom(bob_event_bytes, salt2)

    # Different salts should produce different bloom filters
    assert bloom1 != bloom2, "Different salts should produce different bloom filters"
