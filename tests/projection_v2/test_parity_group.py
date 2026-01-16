"""Parity tests for v2 group projector."""
from __future__ import annotations

import json
import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.group import group as group_module

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


def _insert_group_key(
    db: Database,
    recorded_by: str,
    key_id: str,
    key_bytes: bytes,
    created_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO group_keys
           (key_id, key, created_at, recorded_by)
           VALUES (?, ?, ?, ?)""",
        (key_id, key_bytes, created_at, recorded_by),
    )


def _insert_peer_shared(
    db: Database,
    recorded_by: str,
    peer_shared_id: str,
    peer_id: str,
    public_key_b64: str,
    created_at: int,
    recorded_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO peers_shared
           (peer_shared_id, peer_id, public_key, user_id, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (peer_shared_id, peer_id, public_key_b64, None, created_at, recorded_by, recorded_at),
    )


def _insert_network(
    db: Database,
    recorded_by: str,
    network_id: str,
    network_pubkey_b64: str,
    created_at: int,
    recorded_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO networks
           (network_id, creator_user_id, network_pubkey, signed_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (network_id, '', network_pubkey_b64, network_id, created_at, recorded_by, recorded_at),
    )


def _build_group_key_event() -> tuple[str, dict, bytes, bytes, dict]:
    key_bytes = crypto.generate_secret()
    event_data = {
        'type': 'group_key',
        'key': crypto.b64encode(key_bytes),
    }
    blob = json.dumps(event_data, sort_keys=True).encode()
    key_id = crypto.b64encode(crypto.hash(blob))
    key_data = {
        'id': crypto.b64decode(key_id),
        'key': key_bytes,
        'type': 'symmetric',
    }
    return key_id, event_data, blob, key_bytes, key_data


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
    network_id = crypto.b64encode(crypto.hash(blob))
    return network_id, signed_event, blob, network_private_key, network_public_key


def _build_peer_shared_event(t_ms: int, peer_id: str, public_key_b64: str) -> tuple[str, dict, bytes]:
    invite_private_key, invite_public_key = crypto.generate_keypair()
    invite_id = crypto.b64encode(crypto.hash(invite_public_key))
    event_data = {
        'type': 'peer_shared',
        'public_key': public_key_b64,
        'peer_id': peer_id,
        'invite_id': invite_id,
        'signed_by': invite_id,
        'created_at': t_ms,
    }
    signed_event = crypto.sign_event(event_data, invite_private_key)
    blob = crypto.canonicalize_json(signed_event)
    peer_shared_id = crypto.b64encode(crypto.hash(blob))
    return peer_shared_id, signed_event, blob


def _build_group_event(
    name: str,
    key_id: str,
    key_data: dict,
    signed_by: str,
    signer_type: str,
    signer_private_key: bytes,
    created_at: int,
    db: Database,
    is_main: int = 0,
    network_id: str | None = None,
) -> tuple[str, dict, bytes]:
    event_data = {
        'type': 'group',
        'name': name,
        'signed_by': signed_by,
        'signer_type': signer_type,
        'created_at': created_at,
        'key_id': key_id,
        'is_main': is_main,
    }
    if network_id:
        event_data['network_id'] = network_id
    signed_event = crypto.sign_event(event_data, signer_private_key)
    plaintext = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(plaintext, key_data, db)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_group_legacy(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    blob: bytes,
    event_id: str,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    group_module.project(event_id, recorded_by, recorded_at, db)


def _project_group_v2(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='group',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = group_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_group_parity_peer_signed():
    recorded_by = 'peer1'
    recorded_at = 1200

    key_id, _, _, key_bytes, key_data = _build_group_key_event()

    peer_private_key, peer_public_key = crypto.generate_keypair()
    peer_public_b64 = crypto.b64encode(peer_public_key)
    peer_shared_id, _, peer_shared_blob = _build_peer_shared_event(
        recorded_at,
        peer_id='peer_local',
        public_key_b64=peer_public_b64,
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_group_key(db, recorded_by, key_id, key_bytes, created_at=100)
        _insert_peer_shared(
            db,
            recorded_by,
            peer_shared_id,
            peer_id='peer_local',
            public_key_b64=peer_public_b64,
            created_at=100,
            recorded_at=recorded_at,
        )
        _mark_valid(db, recorded_by, key_id)
        _mark_valid(db, recorded_by, peer_shared_id)

    _store_blob(legacy_db, peer_shared_blob, recorded_at)

    event_id, signed_event, blob = _build_group_event(
        name='private_group',
        key_id=key_id,
        key_data=key_data,
        signed_by=peer_shared_id,
        signer_type='peer_shared',
        signer_private_key=peer_private_key,
        created_at=recorded_at,
        db=legacy_db,
    )

    _project_group_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_group_v2(v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('groups', recorded_by, legacy_db)
    v2_rows = get_table_rows('groups', recorded_by, v2_db)

    assert legacy_rows == v2_rows


def test_group_parity_network_signed():
    recorded_by = 'peer1'
    recorded_at = 1400

    key_id, _, _, key_bytes, key_data = _build_group_key_event()
    network_id, _, network_blob, network_private_key, network_public_key = _build_network_event(1000)

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_group_key(db, recorded_by, key_id, key_bytes, created_at=100)
        _insert_network(
            db,
            recorded_by,
            network_id,
            network_pubkey_b64=crypto.b64encode(network_public_key),
            created_at=1000,
            recorded_at=recorded_at,
        )
        _mark_valid(db, recorded_by, key_id)
        _mark_valid(db, recorded_by, network_id)

    _store_blob(legacy_db, network_blob, recorded_at)

    event_id, signed_event, blob = _build_group_event(
        name='all_users',
        key_id=key_id,
        key_data=key_data,
        signed_by=network_id,
        signer_type='network',
        signer_private_key=network_private_key,
        created_at=recorded_at,
        db=legacy_db,
        is_main=1,
        network_id=network_id,
    )

    _project_group_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_group_v2(v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('groups', recorded_by, legacy_db)
    v2_rows = get_table_rows('groups', recorded_by, v2_db)

    assert legacy_rows == v2_rows
