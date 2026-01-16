"""Parity tests for v2 peer, group_prekey, and observed_address projectors."""
from __future__ import annotations

import json
import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.identity import peer as peer_module
from events.group import group_prekey as group_prekey_module
from events.network import observed_address as observed_address_module

from tests.projection_v2.helpers import get_table_rows


def _new_db() -> Database:
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)
    return db


def _store_blob(db: Database, blob: bytes, t_ms: int) -> str:
    unsafedb = create_unsafe_db(db)
    return store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)


def _mark_valid(db: Database, recorded_by: str, event_id: str) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (event_id, recorded_by),
    )


# =============================================================================
# Peer event tests
# =============================================================================

def _build_peer_event(t_ms: int) -> tuple[str, dict, bytes]:
    """Build a peer event blob."""
    private_key, public_key = crypto.generate_keypair()
    event_data = {
        'type': 'peer',
        'public_key': crypto.b64encode(public_key),
        'private_key': crypto.b64encode(private_key),
        'created_at': t_ms,
    }
    blob = json.dumps(event_data).encode()
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, event_data, blob


def _project_peer_legacy(db: Database, recorded_by: str, recorded_at: int, blob: bytes, event_id: str) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    peer_module.project(event_id, recorded_by, recorded_at, db)


def _project_peer_v2(db: Database, recorded_by: str, recorded_at: int, event_id: str, event_data: dict) -> None:
    # For peer events, resolver should work without signer verification
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='peer',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = peer_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_peer_parity():
    """Test that peer v2 projector produces same result as legacy."""
    recorded_by = 'peer1'
    recorded_at = 1000
    event_id, event_data, blob = _build_peer_event(recorded_at)

    legacy_db = _new_db()
    v2_db = _new_db()

    _project_peer_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_peer_v2(v2_db, recorded_by, recorded_at, event_id, event_data)

    # local_peers is not subjective, so we query without recorded_by filter
    legacy_unsafedb = create_unsafe_db(legacy_db)
    v2_unsafedb = create_unsafe_db(v2_db)

    legacy_rows = list(legacy_unsafedb.query("SELECT peer_id, public_key, created_at FROM local_peers"))
    v2_rows = list(v2_unsafedb.query("SELECT peer_id, public_key, created_at FROM local_peers"))

    assert len(legacy_rows) == 1
    assert len(v2_rows) == 1
    assert legacy_rows[0]['peer_id'] == v2_rows[0]['peer_id']
    assert legacy_rows[0]['public_key'] == v2_rows[0]['public_key']
    assert legacy_rows[0]['created_at'] == v2_rows[0]['created_at']


# =============================================================================
# Group prekey tests
# =============================================================================

def _build_group_prekey_event() -> tuple[str, dict, bytes]:
    """Build a group_prekey event blob (deterministic, no timestamp)."""
    private_key, public_key = crypto.generate_keypair()
    event_data = {
        'type': 'group_prekey',
        'public_key': crypto.b64encode(public_key),
        'private_key': crypto.b64encode(private_key),
    }
    blob = json.dumps(event_data, sort_keys=True).encode()
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, event_data, blob


def _project_group_prekey_legacy(db: Database, recorded_by: str, recorded_at: int, blob: bytes, event_id: str) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    group_prekey_module.project(event_id, recorded_by, recorded_at, db)


def _project_group_prekey_v2(db: Database, recorded_by: str, recorded_at: int, event_id: str, event_data: dict) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='group_prekey',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = group_prekey_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_group_prekey_parity():
    """Test that group_prekey v2 projector produces same result as legacy."""
    recorded_by = 'peer1'
    recorded_at = 2000
    event_id, event_data, blob = _build_group_prekey_event()

    legacy_db = _new_db()
    v2_db = _new_db()

    _project_group_prekey_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_group_prekey_v2(v2_db, recorded_by, recorded_at, event_id, event_data)

    legacy_rows = get_table_rows('group_prekeys', recorded_by, legacy_db)
    v2_rows = get_table_rows('group_prekeys', recorded_by, v2_db)

    assert len(legacy_rows) == 1
    assert len(v2_rows) == 1
    assert legacy_rows[0]['prekey_id'] == v2_rows[0]['prekey_id']
    assert legacy_rows[0]['owner_peer_id'] == v2_rows[0]['owner_peer_id']
    assert legacy_rows[0]['created_at'] == v2_rows[0]['created_at']
    assert legacy_rows[0]['ttl_ms'] == v2_rows[0]['ttl_ms']
    # Keys should be decoded to bytes in both
    assert legacy_rows[0]['public_key'] == v2_rows[0]['public_key']
    assert legacy_rows[0]['private_key'] == v2_rows[0]['private_key']


# =============================================================================
# Observed address tests
# =============================================================================

def _build_observed_address_event(
    t_ms: int,
    observed_peer_id: str,
    signer_peer_shared_id: str,
    signer_private_key: bytes,
    ip: str,
    port: int,
) -> tuple[str, dict, bytes]:
    """Build an observed_address event blob."""
    event_data = {
        'type': 'observed_address',
        'observed_peer_id': observed_peer_id,
        'signed_by': signer_peer_shared_id,
        'signer_type': 'peer_shared',
        'ip': ip,
        'port': port,
        'created_at': t_ms,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _setup_peer_shared(db: Database, recorded_by: str, peer_shared_id: str, public_key: bytes) -> None:
    """Set up a peer_shared record for signature verification."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO peers_shared
           (peer_shared_id, peer_id, public_key, user_id, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            peer_shared_id,
            'local_peer',
            crypto.b64encode(public_key),
            'user1',
            1000,
            recorded_by,
            1000,
        ),
    )
    _mark_valid(db, recorded_by, peer_shared_id)


def _project_observed_address_legacy(db: Database, recorded_by: str, recorded_at: int, blob: bytes, event_id: str) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    observed_address_module.project(event_id, recorded_by, recorded_at, db)


def _project_observed_address_v2(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='observed_address',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = observed_address_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_observed_address_parity():
    """Test that observed_address v2 projector produces same result as legacy."""
    recorded_by = 'peer1'
    recorded_at = 3000

    # Create signer identity
    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    observed_peer_id = 'observed_peer_123'

    event_id, signed_event, blob = _build_observed_address_event(
        t_ms=recorded_at,
        observed_peer_id=observed_peer_id,
        signer_peer_shared_id=signer_peer_shared_id,
        signer_private_key=signer_private_key,
        ip='192.168.1.100',
        port=8080,
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    # Set up peer_shared for signature verification in both DBs
    _setup_peer_shared(legacy_db, recorded_by, signer_peer_shared_id, signer_public_key)
    _setup_peer_shared(v2_db, recorded_by, signer_peer_shared_id, signer_public_key)

    _project_observed_address_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_observed_address_v2(v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('network_addresses', recorded_by, legacy_db)
    v2_rows = get_table_rows('network_addresses', recorded_by, v2_db)

    assert len(legacy_rows) == 1
    assert len(v2_rows) == 1
    assert legacy_rows[0]['address_id'] == v2_rows[0]['address_id']
    assert legacy_rows[0]['observed_peer_id'] == v2_rows[0]['observed_peer_id']
    assert legacy_rows[0]['signed_by'] == v2_rows[0]['signed_by']
    assert legacy_rows[0]['ip'] == v2_rows[0]['ip']
    assert legacy_rows[0]['port'] == v2_rows[0]['port']
    assert legacy_rows[0]['created_at'] == v2_rows[0]['created_at']
