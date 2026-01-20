"""Parity tests for v2 network/admin projectors."""
from __future__ import annotations

import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.identity import admin as admin_module
from events.identity import network as network_module

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


def _add_trust_anchor(db: Database, recorded_by: str, network_id: str, t_ms: int) -> None:
    """Add trust anchor for network (required before network can project)."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        "INSERT OR IGNORE INTO trust_anchors (network_id, recorded_by, created_at) VALUES (?, ?, ?)",
        (network_id, recorded_by, t_ms),
    )


def _build_network_event(t_ms: int) -> tuple[str, dict, bytes, bytes, bytes]:
    network_private_key, network_public_key = crypto.generate_keypair()
    event_data = {
        'type': 'network',
        'network_pubkey': crypto.b64encode(network_public_key),
        'signer_type': 'network',
        'created_at': t_ms,
    }
    signed_event = crypto.sign_event(event_data, network_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob, network_private_key, network_public_key


def _build_admin_event(
    t_ms: int,
    user_id: str,
    network_id: str,
    signed_by: str,
    signer_type: str,
    signer_private_key: bytes,
    admin_grant: str | None = None,
) -> tuple[str, dict, bytes]:
    event_data = {
        'type': 'admin',
        'user_id': user_id,
        'network_id': network_id,
        'signed_by': signed_by,
        'signer_type': signer_type,
        'created_at': t_ms,
    }
    if admin_grant:
        event_data['admin_grant'] = admin_grant
    signed_event = crypto.sign_event(event_data, signer_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_network_legacy(db: Database, recorded_by: str, recorded_at: int, blob: bytes, event_id: str) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    # Trust anchor required before network can project (uniform model)
    _add_trust_anchor(db, recorded_by, event_id, recorded_at)
    network_module.project(event_id, recorded_by, recorded_at, db)
    _mark_valid(db, recorded_by, event_id)


def _project_network_v2(db: Database, recorded_by: str, recorded_at: int, event_id: str, event_data: dict) -> None:
    # Trust anchor required before network can project (uniform model)
    _add_trust_anchor(db, recorded_by, event_id, recorded_at)
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='network',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = network_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def _project_admin_legacy(db: Database, recorded_by: str, recorded_at: int, blob: bytes, event_id: str) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    admin_module.project(event_id, recorded_by, recorded_at, db)


def _project_admin_v2(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='admin',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = admin_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_network_parity():
    recorded_by = 'peer1'
    recorded_at = 1000
    event_id, signed_event, blob, _, _ = _build_network_event(recorded_at)

    legacy_db = _new_db()
    v2_db = _new_db()

    _project_network_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_network_v2(v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('networks', recorded_by, legacy_db)
    v2_rows = get_table_rows('networks', recorded_by, v2_db)

    assert legacy_rows == v2_rows


def test_admin_bootstrap_parity():
    recorded_by = 'peer1'
    recorded_at = 2000
    network_id, network_event, network_blob, network_priv, _ = _build_network_event(1000)

    legacy_db = _new_db()
    v2_db = _new_db()

    _project_network_legacy(legacy_db, recorded_by, recorded_at, network_blob, network_id)
    _project_network_legacy(v2_db, recorded_by, recorded_at, network_blob, network_id)

    admin_id, admin_event, admin_blob = _build_admin_event(
        t_ms=recorded_at,
        user_id='user_bootstrap',
        network_id=network_id,
        signed_by=network_id,
        signer_type='network',
        signer_private_key=network_priv,
    )

    _project_admin_legacy(legacy_db, recorded_by, recorded_at, admin_blob, admin_id)
    _project_admin_v2(v2_db, recorded_by, recorded_at, admin_id, admin_event)

    legacy_admins = get_table_rows('admins', recorded_by, legacy_db)
    v2_admins = get_table_rows('admins', recorded_by, v2_db)
    assert legacy_admins == v2_admins

    legacy_networks = get_table_rows('networks', recorded_by, legacy_db)
    v2_networks = get_table_rows('networks', recorded_by, v2_db)
    assert legacy_networks == v2_networks


def test_admin_ongoing_parity():
    recorded_by = 'peer1'
    recorded_at = 3000
    network_id, network_event, network_blob, _, _ = _build_network_event(1000)

    legacy_db = _new_db()
    v2_db = _new_db()

    _project_network_legacy(legacy_db, recorded_by, recorded_at, network_blob, network_id)
    _project_network_legacy(v2_db, recorded_by, recorded_at, network_blob, network_id)

    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    signer_user_id = 'user_signer'

    for db in (legacy_db, v2_db):
        safedb = create_safe_db(db, recorded_by=recorded_by)
        safedb.execute(
            """INSERT OR IGNORE INTO peers_shared
               (peer_shared_id, peer_id, public_key, user_id, created_at, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                signer_peer_shared_id,
                'peer_local',
                crypto.b64encode(signer_public_key),
                signer_user_id,
                recorded_at,
                recorded_by,
                recorded_at,
            ),
        )
        _mark_valid(db, recorded_by, signer_peer_shared_id)

    admin_grant_id = crypto.b64encode(crypto.hash(b'admin_grant_seed'))
    for db in (legacy_db, v2_db):
        safedb = create_safe_db(db, recorded_by=recorded_by)
        safedb.execute(
            """INSERT OR IGNORE INTO admins
               (admin_id, network_id, user_id, signed_by, admin_grant, created_at, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                admin_grant_id,
                network_id,
                signer_user_id,
                network_id,
                None,
                1500,
                recorded_by,
                recorded_at,
            ),
        )
        _mark_valid(db, recorded_by, admin_grant_id)

    admin_id, admin_event, admin_blob = _build_admin_event(
        t_ms=recorded_at,
        user_id='user_target',
        network_id=network_id,
        signed_by=signer_peer_shared_id,
        signer_type='peer_shared',
        signer_private_key=signer_private_key,
        admin_grant=admin_grant_id,
    )

    _project_admin_legacy(legacy_db, recorded_by, recorded_at, admin_blob, admin_id)
    _project_admin_v2(v2_db, recorded_by, recorded_at, admin_id, admin_event)

    legacy_admins = get_table_rows('admins', recorded_by, legacy_db)
    v2_admins = get_table_rows('admins', recorded_by, v2_db)
    assert legacy_admins == v2_admins
