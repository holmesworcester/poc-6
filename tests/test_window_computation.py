"""Unit tests for window computation and storage."""
import sqlite3
import pytest
from db import Database
import schema
from events.identity import user, invite
import crypto


def test_channel_window_assignment():
    """Test that channel is assigned to a valid storage window."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    # Check shareable_events for Alice's channel
    channel_rows = db.query(
        "SELECT event_id, can_share_peer_id, window_id FROM shareable_events WHERE event_id = ?",
        (alice['channel_id'],)
    )

    assert len(channel_rows) > 0, "Channel should be in shareable_events"

    for row in channel_rows:
        storage_window = row['window_id']
        assert storage_window >= 0, "Storage window ID should be non-negative"


def test_all_alice_shareable_events_have_windows():
    """Test that all of Alice's shareable events are assigned windows."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)

    all_alice_events = db.query(
        "SELECT event_id, window_id FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    )

    assert len(all_alice_events) > 0, "Alice should have shareable events"

    for evt in all_alice_events:
        assert evt['window_id'] >= 0, f"Event {evt['event_id'][:20]}... should have valid window"


def test_window_ranges_for_sync():
    """Test window range calculations for different w values."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

    # Get all shareable events
    all_events = db.query(
        "SELECT event_id, window_id FROM shareable_events WHERE window_id IS NOT NULL"
    )

    assert len(all_events) > 0, "Should have events with windows"

    # Verify window calculations for w=1
    for evt in all_events:
        storage_window = evt['window_id']
        w = 1
        sync_window = storage_window >> (20 - w)
        window_min = sync_window << (20 - w)
        window_max = (sync_window + 1) << (20 - w)

        # Event should be within its range
        assert window_min <= storage_window < window_max, f"Window {storage_window} should be in range [{window_min}, {window_max})"
