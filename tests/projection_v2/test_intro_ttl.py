"""Tests for intro TTL handling."""
from core import crypto, store
from core.db import create_safe_db
from events.identity import peer as peer_module
from events.network import intro as intro_module


def test_intro_stale_is_dropped(fresh_db_with_alice):
    db, alice = fresh_db_with_alice

    created_at = 1000
    recorded_at = created_at + intro_module.INTRO_TTL_MS + 1

    event_data = {
        'type': 'network_intro',
        'signed_by': alice['peer_shared_id'],
        'signer_type': 'peer_shared',
        'peer1_id': alice['peer_id'],
        'peer2_id': alice['peer_id'],
        'created_at': created_at,
    }

    private_key = peer_module.get_private_key(alice['peer_id'], alice['peer_id'], db)
    signed_event = crypto.sign_event(event_data, private_key)
    blob = crypto.canonicalize_json(signed_event)

    intro_id = store.event(blob, alice['peer_id'], recorded_at, db)

    safedb = create_safe_db(db, recorded_by=alice['peer_id'])
    pending = safedb.query_one(
        "SELECT 1 FROM pending_intros WHERE intro_id = ? AND recorded_by = ?",
        (intro_id, alice['peer_id']),
    )
    assert pending is None

    valid = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (intro_id, alice['peer_id']),
    )
    assert valid is not None
