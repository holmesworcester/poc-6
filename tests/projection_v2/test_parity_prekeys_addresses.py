"""Parity tests for v2 prekey and address projectors."""
from __future__ import annotations

import json
import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.network import self_address as self_address_module
from events.network import connection_prekey as transit_prekey_module
from events.network import connection_prekey_shared as transit_prekey_shared_module
from events.group import group_prekey_shared as group_prekey_shared_module

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


# =============================================================================
# Self Address Tests
# =============================================================================

def _build_self_address_event(t_ms: int, peer_shared_id: str, signer_private_key: bytes, ip: str, port: int) -> tuple[str, dict, bytes]:
    """Build a self_address event blob."""
    event_data = {
        'type': 'self_address',
        'peer_id': peer_shared_id,
        'signed_by': peer_shared_id,
        'signer_type': 'peer_shared',
        'ip': ip,
        'port': port,
        'created_at': t_ms,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_self_address_legacy(db: Database, recorded_by: str, recorded_at: int, blob: bytes, event_id: str) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    self_address_module.project(event_id, recorded_by, recorded_at, db)


def _project_self_address_v2(db: Database, recorded_by: str, recorded_at: int, event_id: str, event_data: dict) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='self_address',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = self_address_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_self_address_parity():
    """Test that self_address v2 projector produces same result as legacy."""
    recorded_by = 'peer1'
    recorded_at = 1000

    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))

    event_id, signed_event, blob = _build_self_address_event(
        t_ms=recorded_at,
        peer_shared_id=signer_peer_shared_id,
        signer_private_key=signer_private_key,
        ip='192.168.1.1',
        port=9000,
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    _setup_peer_shared(legacy_db, recorded_by, signer_peer_shared_id, signer_public_key)
    _setup_peer_shared(v2_db, recorded_by, signer_peer_shared_id, signer_public_key)

    _project_self_address_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_self_address_v2(v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('addresses', recorded_by, legacy_db)
    v2_rows = get_table_rows('addresses', recorded_by, v2_db)

    assert len(legacy_rows) == 1
    assert len(v2_rows) == 1
    assert legacy_rows[0]['address_id'] == v2_rows[0]['address_id']
    assert legacy_rows[0]['peer_shared_id'] == v2_rows[0]['peer_shared_id']
    assert legacy_rows[0]['ip'] == v2_rows[0]['ip']
    assert legacy_rows[0]['port'] == v2_rows[0]['port']


# =============================================================================
# Transit Prekey Tests
# =============================================================================

def _build_transit_prekey_event() -> tuple[str, dict, bytes]:
    """Build a connection_prekey event blob (local-only, unsigned)."""
    private_key, public_key = crypto.generate_keypair()
    peer_id = 'local_peer_123'
    t_ms = 2000
    event_data = {
        'type': 'connection_prekey',
        'public_key': crypto.b64encode(public_key),
        'private_key': crypto.b64encode(private_key),
        'signed_by': peer_id,
        'created_at': t_ms,
    }
    blob = json.dumps(event_data).encode()
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, event_data, blob


def _project_transit_prekey_legacy(db: Database, recorded_by: str, recorded_at: int, blob: bytes, event_id: str) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    transit_prekey_module.project(event_id, recorded_by, recorded_at, db)


def _project_transit_prekey_v2(db: Database, recorded_by: str, recorded_at: int, event_id: str, event_data: dict) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='connection_prekey',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = transit_prekey_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_transit_prekey_parity():
    """Test that transit_prekey v2 projector produces same result as legacy."""
    recorded_by = 'peer1'
    recorded_at = 2000
    event_id, event_data, blob = _build_transit_prekey_event()
    owner_peer_id = event_data.get('signed_by')

    legacy_db = _new_db()
    v2_db = _new_db()

    # Create local_peers record for the owner (required by foreign key)
    for db in (legacy_db, v2_db):
        unsafedb = create_unsafe_db(db)
        unsafedb.execute(
            "INSERT INTO local_peers (peer_id, public_key, private_key, created_at) VALUES (?, ?, ?, ?)",
            (owner_peer_id, crypto.b64encode(b'pubkey'), b'privkey', recorded_at)
        )

    _project_transit_prekey_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_transit_prekey_v2(v2_db, recorded_by, recorded_at, event_id, event_data)

    # connection_prekeys is not subjective
    legacy_unsafedb = create_unsafe_db(legacy_db)
    v2_unsafedb = create_unsafe_db(v2_db)

    legacy_rows = list(legacy_unsafedb.query("SELECT connection_prekey_id, owner_peer_id, created_at, ttl_ms FROM connection_prekeys"))
    v2_rows = list(v2_unsafedb.query("SELECT connection_prekey_id, owner_peer_id, created_at, ttl_ms FROM connection_prekeys"))

    assert len(legacy_rows) == 1
    assert len(v2_rows) == 1
    assert legacy_rows[0]['connection_prekey_id'] == v2_rows[0]['connection_prekey_id']
    assert legacy_rows[0]['owner_peer_id'] == v2_rows[0]['owner_peer_id']
    assert legacy_rows[0]['created_at'] == v2_rows[0]['created_at']
    assert legacy_rows[0]['ttl_ms'] == v2_rows[0]['ttl_ms']


# =============================================================================
# Transit Prekey Shared Tests
# =============================================================================

def _build_transit_prekey_shared_event(
    t_ms: int,
    connection_prekey_id: str,
    peer_shared_id: str,
    signer_private_key: bytes,
) -> tuple[str, dict, bytes]:
    """Build a connection_prekey_shared event blob."""
    event_data = {
        'type': 'connection_prekey_shared',
        'connection_prekey_id': connection_prekey_id,
        'peer_id': peer_shared_id,
        'public_key': crypto.b64encode(crypto.generate_keypair()[1]),  # Dummy public key
        'signed_by': peer_shared_id,
        'signer_type': 'peer_shared',
        'created_at': t_ms,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_transit_prekey_shared_legacy(db: Database, recorded_by: str, recorded_at: int, blob: bytes, event_id: str) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    transit_prekey_shared_module.project(event_id, recorded_by, recorded_at, db)


def _project_transit_prekey_shared_v2(db: Database, recorded_by: str, recorded_at: int, event_id: str, event_data: dict) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='connection_prekey_shared',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = transit_prekey_shared_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_transit_prekey_shared_parity():
    """Test that transit_prekey_shared v2 projector produces same result as legacy."""
    recorded_by = 'peer1'
    recorded_at = 3000

    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    connection_prekey_id = 'transit_prekey_123'

    event_id, signed_event, blob = _build_transit_prekey_shared_event(
        t_ms=recorded_at,
        connection_prekey_id=connection_prekey_id,
        peer_shared_id=signer_peer_shared_id,
        signer_private_key=signer_private_key,
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    _setup_peer_shared(legacy_db, recorded_by, signer_peer_shared_id, signer_public_key)
    _setup_peer_shared(v2_db, recorded_by, signer_peer_shared_id, signer_public_key)

    _project_transit_prekey_shared_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_transit_prekey_shared_v2(v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('connection_prekeys_shared', recorded_by, legacy_db)
    v2_rows = get_table_rows('connection_prekeys_shared', recorded_by, v2_db)

    assert len(legacy_rows) == 1
    assert len(v2_rows) == 1
    assert legacy_rows[0]['connection_prekey_shared_id'] == v2_rows[0]['connection_prekey_shared_id']
    assert legacy_rows[0]['connection_prekey_id'] == v2_rows[0]['connection_prekey_id']
    assert legacy_rows[0]['peer_id'] == v2_rows[0]['peer_id']


# =============================================================================
# Group Prekey Shared Tests
# =============================================================================

def _build_group_prekey_shared_event(
    t_ms: int,
    group_prekey_id: str,
    peer_shared_id: str,
    signer_private_key: bytes,
) -> tuple[str, dict, bytes]:
    """Build a group_prekey_shared event blob."""
    event_data = {
        'type': 'group_prekey_shared',
        'group_prekey_id': group_prekey_id,
        'peer_id': peer_shared_id,
        'public_key': crypto.b64encode(crypto.generate_keypair()[1]),  # Dummy public key
        'signed_by': peer_shared_id,
        'signer_type': 'peer_shared',
        'created_at': t_ms,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_group_prekey_shared_legacy(db: Database, recorded_by: str, recorded_at: int, blob: bytes, event_id: str) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    group_prekey_shared_module.project(event_id, recorded_by, recorded_at, db)


def _project_group_prekey_shared_v2(db: Database, recorded_by: str, recorded_at: int, event_id: str, event_data: dict) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='group_prekey_shared',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = group_prekey_shared_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_group_prekey_shared_parity():
    """Test that group_prekey_shared v2 projector produces same result as legacy."""
    recorded_by = 'peer1'
    recorded_at = 4000

    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    group_prekey_id = 'group_prekey_123'

    event_id, signed_event, blob = _build_group_prekey_shared_event(
        t_ms=recorded_at,
        group_prekey_id=group_prekey_id,
        peer_shared_id=signer_peer_shared_id,
        signer_private_key=signer_private_key,
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    _setup_peer_shared(legacy_db, recorded_by, signer_peer_shared_id, signer_public_key)
    _setup_peer_shared(v2_db, recorded_by, signer_peer_shared_id, signer_public_key)

    _project_group_prekey_shared_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_group_prekey_shared_v2(v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('group_prekeys_shared', recorded_by, legacy_db)
    v2_rows = get_table_rows('group_prekeys_shared', recorded_by, v2_db)

    assert len(legacy_rows) == 1
    assert len(v2_rows) == 1
    assert legacy_rows[0]['group_prekey_shared_id'] == v2_rows[0]['group_prekey_shared_id']
    assert legacy_rows[0]['group_prekey_id'] == v2_rows[0]['group_prekey_id']
    assert legacy_rows[0]['peer_id'] == v2_rows[0]['peer_id']
