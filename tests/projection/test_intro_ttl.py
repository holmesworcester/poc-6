"""Tests for intro TTL handling."""
from core import crypto, store
from core.db import create_safe_db
from events.identity import peer as peer_module
from events.network import intro as intro_module


def test_intro_stale_is_dropped(fresh_db_with_alice):
    """Test that stale intros (past TTL) are marked valid but not pending.

    Uses wire format encoder directly because we need precise control over
    created_at vs recorded_at timing that the high-level API doesn't support.
    """
    db, alice = fresh_db_with_alice

    created_at = 1000
    recorded_at = created_at + intro_module.INTRO_TTL_MS + 1

    # Get signing key
    private_key = peer_module.get_private_key(alice['peer_id'], alice['peer_id'], db)

    # Create intro with specific timestamp using wire format
    blob = intro_module.encode_wire_event(
        peer1_id_b64=alice['peer_id'],
        peer2_id_b64=alice['peer_id'],
        signed_by_b64=alice['peer_shared_id'],
        signer_type='peer_shared',
        created_at_ms=created_at,
        private_key=private_key,
    )

    # Store with recorded_at > created_at + TTL to trigger staleness
    intro_id = store.event(blob, alice['peer_id'], recorded_at, db)

    safedb = create_safe_db(db, recorded_by=alice['peer_id'])

    # Stale intros should NOT be in pending_intros
    pending = safedb.query_one(
        "SELECT 1 FROM pending_intros WHERE intro_id = ? AND recorded_by = ?",
        (intro_id, alice['peer_id']),
    )
    assert pending is None, "Stale intro should not be pending"

    # But should still be marked as valid (event is well-formed)
    valid = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (intro_id, alice['peer_id']),
    )
    assert valid is not None, "Stale intro should still be valid"
