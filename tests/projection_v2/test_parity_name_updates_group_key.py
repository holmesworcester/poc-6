"""Parity tests for v2 name updates and group_key projectors."""
from __future__ import annotations

import json
import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.group import group_key as group_key_module
from events.identity import network_name_update as network_name_module
from events.identity import peer_name_update as peer_name_module
from events.identity import username_update as username_module

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


def _insert_user(
    db: Database,
    recorded_by: str,
    user_id: str,
    user_pubkey_b64: str,
    created_at: int,
    recorded_at: int,
    network_id: str | None = None,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO users
           (user_id, name, network_id, created_at, user_pubkey, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, '', network_id, created_at, user_pubkey_b64, recorded_by, recorded_at),
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


def _insert_peer_shared(
    db: Database,
    recorded_by: str,
    peer_shared_id: str,
    peer_id: str,
    public_key_b64: str,
    user_id: str | None,
    created_at: int,
    recorded_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO peers_shared
           (peer_shared_id, peer_id, public_key, user_id, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (peer_shared_id, peer_id, public_key_b64, user_id, created_at, recorded_by, recorded_at),
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


def _build_name_update_event(
    event_type: str,
    entity_field: str,
    entity_id: str,
    name: str,
    key_id: str,
    key_data: dict,
    signed_by: str,
    signer_private_key: bytes,
    created_at: int,
    db: Database,
    global_count: int = 0,
) -> tuple[str, dict, bytes]:
    event_data = {
        'type': event_type,
        entity_field: entity_id,
        'name': name,
        'key_id': key_id,
        'global_count': global_count,
        'signed_by': signed_by,
        'signer_type': 'peer_shared',
        'created_at': created_at,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    plaintext = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(plaintext, key_data, db)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_group_key_legacy(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    blob: bytes,
    event_id: str,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    group_key_module.project(event_id, recorded_by, recorded_at, db)


def _project_group_key_v2(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='group_key',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = group_key_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def _project_name_legacy(
    module,
    db: Database,
    recorded_by: str,
    recorded_at: int,
    blob: bytes,
    event_id: str,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    module.project(event_id, recorded_by, recorded_at, db)


def _project_name_v2(
    module,
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type=event_data.get('type'),
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_group_key_parity():
    recorded_by = 'peer1'
    recorded_at = 1000
    key_id, event_data, blob, _, _ = _build_group_key_event()

    legacy_db = _new_db()
    v2_db = _new_db()

    _project_group_key_legacy(legacy_db, recorded_by, recorded_at, blob, key_id)
    _project_group_key_v2(v2_db, recorded_by, recorded_at, key_id, event_data)

    legacy_rows = get_table_rows('group_keys', recorded_by, legacy_db)
    v2_rows = get_table_rows('group_keys', recorded_by, v2_db)

    assert legacy_rows == v2_rows


def test_username_update_parity():
    recorded_by = 'peer1'
    recorded_at = 2000
    user_id = crypto.b64encode(crypto.hash(b'user_seed'))

    key_id, _, _, key_bytes, key_data = _build_group_key_event()

    signer_private_key, signer_public_key = crypto.generate_keypair()
    peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    peer_shared_pub_b64 = crypto.b64encode(signer_public_key)

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_group_key(db, recorded_by, key_id, key_bytes, created_at=100)
        _insert_user(
            db,
            recorded_by,
            user_id,
            user_pubkey_b64=crypto.b64encode(crypto.hash(b'user_pubkey')),
            created_at=100,
            recorded_at=recorded_at,
        )
        _insert_peer_shared(
            db,
            recorded_by,
            peer_shared_id,
            peer_id='peer_local',
            public_key_b64=peer_shared_pub_b64,
            user_id=None,
            created_at=100,
            recorded_at=recorded_at,
        )
        _mark_valid(db, recorded_by, key_id)
        _mark_valid(db, recorded_by, user_id)
        _mark_valid(db, recorded_by, peer_shared_id)

    event_id, signed_event, blob = _build_name_update_event(
        event_type='username_update',
        entity_field='user_id',
        entity_id=user_id,
        name='alice',
        key_id=key_id,
        key_data=key_data,
        signed_by=peer_shared_id,
        signer_private_key=signer_private_key,
        created_at=recorded_at,
        db=legacy_db,
    )

    _project_name_legacy(username_module, legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_name_v2(username_module, v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('user_names', recorded_by, legacy_db)
    v2_rows = get_table_rows('user_names', recorded_by, v2_db)

    assert legacy_rows == v2_rows


def test_peer_name_update_parity():
    recorded_by = 'peer1'
    recorded_at = 2500

    key_id, _, _, key_bytes, key_data = _build_group_key_event()

    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    signer_peer_shared_pub_b64 = crypto.b64encode(signer_public_key)

    _, target_public_key = crypto.generate_keypair()
    target_peer_shared_id = crypto.b64encode(crypto.hash(target_public_key))
    target_peer_shared_pub_b64 = crypto.b64encode(target_public_key)

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_group_key(db, recorded_by, key_id, key_bytes, created_at=150)
        _insert_peer_shared(
            db,
            recorded_by,
            signer_peer_shared_id,
            peer_id='peer_signer_local',
            public_key_b64=signer_peer_shared_pub_b64,
            user_id=None,
            created_at=150,
            recorded_at=recorded_at,
        )
        _insert_peer_shared(
            db,
            recorded_by,
            target_peer_shared_id,
            peer_id='peer_target_local',
            public_key_b64=target_peer_shared_pub_b64,
            user_id=None,
            created_at=150,
            recorded_at=recorded_at,
        )
        _mark_valid(db, recorded_by, key_id)
        _mark_valid(db, recorded_by, signer_peer_shared_id)
        _mark_valid(db, recorded_by, target_peer_shared_id)

    event_id, signed_event, blob = _build_name_update_event(
        event_type='peer_name_update',
        entity_field='peer_id',
        entity_id=target_peer_shared_id,
        name='Device X',
        key_id=key_id,
        key_data=key_data,
        signed_by=signer_peer_shared_id,
        signer_private_key=signer_private_key,
        created_at=recorded_at,
        db=legacy_db,
    )

    _project_name_legacy(peer_name_module, legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_name_v2(peer_name_module, v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('peer_names', recorded_by, legacy_db)
    v2_rows = get_table_rows('peer_names', recorded_by, v2_db)

    assert legacy_rows == v2_rows


def test_network_name_update_parity():
    recorded_by = 'peer1'
    recorded_at = 3000
    network_id = crypto.b64encode(crypto.hash(b'network_seed'))

    key_id, _, _, key_bytes, key_data = _build_group_key_event()

    signer_private_key, signer_public_key = crypto.generate_keypair()
    peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    peer_shared_pub_b64 = crypto.b64encode(signer_public_key)

    _, network_public_key = crypto.generate_keypair()
    network_pub_b64 = crypto.b64encode(network_public_key)

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_group_key(db, recorded_by, key_id, key_bytes, created_at=200)
        _insert_network(
            db,
            recorded_by,
            network_id,
            network_pubkey_b64=network_pub_b64,
            created_at=200,
            recorded_at=recorded_at,
        )
        _insert_peer_shared(
            db,
            recorded_by,
            peer_shared_id,
            peer_id='peer_local',
            public_key_b64=peer_shared_pub_b64,
            user_id=None,
            created_at=200,
            recorded_at=recorded_at,
        )
        _mark_valid(db, recorded_by, key_id)
        _mark_valid(db, recorded_by, network_id)
        _mark_valid(db, recorded_by, peer_shared_id)

    event_id, signed_event, blob = _build_name_update_event(
        event_type='network_name_update',
        entity_field='network_id',
        entity_id=network_id,
        name='AcmeNet',
        key_id=key_id,
        key_data=key_data,
        signed_by=peer_shared_id,
        signer_private_key=signer_private_key,
        created_at=recorded_at,
        db=legacy_db,
    )

    _project_name_legacy(network_name_module, legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_name_v2(network_name_module, v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('network_names', recorded_by, legacy_db)
    v2_rows = get_table_rows('network_names', recorded_by, v2_db)

    assert legacy_rows == v2_rows
