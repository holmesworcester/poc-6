"""Parity tests for v2 group_member projector."""
from __future__ import annotations

import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.group import group_member as group_member_module

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


def _insert_group(
    db: Database,
    recorded_by: str,
    group_id: str,
    key_id: str,
    signed_by: str,
    created_at: int,
    recorded_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO groups
           (group_id, name, signed_by, created_at, key_id, is_main, network_id, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (group_id, "Test Group", signed_by, created_at, key_id, 0, "", recorded_by, recorded_at),
    )


def _insert_user(
    db: Database,
    recorded_by: str,
    user_id: str,
    network_id: str,
    user_pubkey_b64: str,
    created_at: int,
    recorded_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO users
           (user_id, name, network_id, created_at, user_pubkey, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, "Member", network_id, created_at, user_pubkey_b64, recorded_by, recorded_at),
    )


def _insert_peer_shared(
    db: Database,
    recorded_by: str,
    peer_shared_id: str,
    peer_id: str,
    public_key_b64: str,
    user_id: str,
    created_at: int,
    recorded_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO peers_shared
           (peer_shared_id, peer_id, public_key, user_id, device_name, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (peer_shared_id, peer_id, public_key_b64, user_id, None, created_at, recorded_by, recorded_at),
    )


def _insert_admin(
    db: Database,
    recorded_by: str,
    admin_id: str,
    network_id: str,
    user_id: str,
    signed_by: str,
    created_at: int,
    recorded_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO admins
           (admin_id, network_id, user_id, signed_by, admin_grant, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (admin_id, network_id, user_id, signed_by, None, created_at, recorded_by, recorded_at),
    )


def _build_group_member_event(
    group_id: str,
    user_id: str,
    added_by: str,
    admin_grant: str,
    signer_private_key: bytes,
    created_at: int,
    key_data: dict,
    db: Database,
) -> tuple[str, dict, bytes]:
    event_data = {
        'type': 'group_member',
        'group_id': group_id,
        'user_id': user_id,
        'added_by': added_by,
        'signed_by': added_by,
        'signer_type': 'peer_shared',
        'admin_grant': admin_grant,
        'created_at': created_at,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    plaintext = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(plaintext, key_data, db)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_group_member_legacy(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    blob: bytes,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    group_member_module.project(event_id, recorded_by, recorded_at, db)


def _project_group_member_v2(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='group_member',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = group_member_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_group_member_parity():
    recorded_by = 'peer1'
    recorded_at = 2000
    created_at = 1500

    key_bytes = crypto.generate_secret()
    group_key_event = {
        'type': 'group_key',
        'key': crypto.b64encode(key_bytes),
    }
    key_blob = crypto.canonicalize_json(group_key_event)
    key_id = crypto.b64encode(crypto.hash(key_blob))
    key_data = {
        'id': crypto.b64decode(key_id),
        'key': key_bytes,
        'type': 'symmetric',
    }

    group_id = 'group_1'
    network_id = 'network_1'
    member_user_id = 'user_member'
    admin_user_id = 'user_admin'

    admin_private_key, admin_public_key = crypto.generate_keypair()
    admin_public_b64 = crypto.b64encode(admin_public_key)
    admin_peer_shared_id = crypto.b64encode(crypto.hash(admin_public_key))

    _, user_public_key = crypto.generate_keypair()
    user_public_b64 = crypto.b64encode(user_public_key)

    admin_id = 'admin_grant_1'

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_group_key(db, recorded_by, key_id, key_bytes, created_at=1000)
        _insert_group(db, recorded_by, group_id, key_id, admin_peer_shared_id, created_at, recorded_at)
        _insert_user(db, recorded_by, member_user_id, network_id, user_public_b64, created_at, recorded_at)
        _insert_peer_shared(
            db,
            recorded_by,
            admin_peer_shared_id,
            peer_id='peer_admin',
            public_key_b64=admin_public_b64,
            user_id=admin_user_id,
            created_at=created_at,
            recorded_at=recorded_at,
        )
        _insert_admin(
            db,
            recorded_by,
            admin_id,
            network_id,
            admin_user_id,
            admin_peer_shared_id,
            created_at,
            recorded_at,
        )
        _mark_valid(db, recorded_by, group_id)
        _mark_valid(db, recorded_by, member_user_id)
        _mark_valid(db, recorded_by, admin_peer_shared_id)
        _mark_valid(db, recorded_by, admin_id)

    event_id, event_data, blob = _build_group_member_event(
        group_id=group_id,
        user_id=member_user_id,
        added_by=admin_peer_shared_id,
        admin_grant=admin_id,
        signer_private_key=admin_private_key,
        created_at=created_at,
        key_data=key_data,
        db=legacy_db,
    )

    _project_group_member_legacy(legacy_db, recorded_by, recorded_at, event_id, blob)
    _project_group_member_v2(v2_db, recorded_by, recorded_at, event_id, event_data)

    legacy_rows = get_table_rows('group_members', recorded_by, legacy_db)
    v2_rows = get_table_rows('group_members', recorded_by, v2_db)

    assert len(legacy_rows) == 1
    assert len(v2_rows) == 1
    assert legacy_rows[0]['member_id'] == v2_rows[0]['member_id']
    assert legacy_rows[0]['group_id'] == v2_rows[0]['group_id']
    assert legacy_rows[0]['user_id'] == v2_rows[0]['user_id']
    assert legacy_rows[0]['added_by'] == v2_rows[0]['added_by']
    assert legacy_rows[0]['created_at'] == v2_rows[0]['created_at']
    assert legacy_rows[0]['recorded_by'] == v2_rows[0]['recorded_by']
    assert legacy_rows[0]['recorded_at'] == v2_rows[0]['recorded_at']
