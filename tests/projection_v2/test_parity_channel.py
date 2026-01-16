"""Parity tests for v2 channel projector."""
from __future__ import annotations

import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.content import channel as channel_module

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


def _insert_peer_shared(
    db: Database,
    recorded_by: str,
    peer_shared_id: str,
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
        (
            peer_shared_id,
            recorded_by,
            public_key_b64,
            user_id,
            None,
            created_at,
            recorded_by,
            recorded_at,
        ),
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
        (
            group_id,
            'all_users',
            signed_by,
            created_at,
            key_id,
            1,
            'network_1',
            recorded_by,
            recorded_at,
        ),
    )


def _insert_admin(
    db: Database,
    recorded_by: str,
    admin_id: str,
    user_id: str,
    created_at: int,
    recorded_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO admins
           (admin_id, network_id, user_id, signed_by, admin_grant, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            admin_id,
            'network_1',
            user_id,
            'network_1',
            None,
            created_at,
            recorded_by,
            recorded_at,
        ),
    )


def _build_channel_event(
    t_ms: int,
    name: str,
    group_id: str,
    signed_by: str,
    signer_private_key: bytes,
    admin_grant: str,
    disappearing_time_ms: int,
    is_main: int,
) -> tuple[str, dict, bytes]:
    event_data = {
        'type': 'channel',
        'name': name,
        'group_id': group_id,
        'signed_by': signed_by,
        'signer_type': 'peer_shared',
        'created_at': t_ms,
        'disappearing_time_ms': disappearing_time_ms,
        'is_main': is_main,
        'admin_grant': admin_grant,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_channel_legacy(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    blob: bytes,
    event_id: str,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    channel_module.project(event_id, recorded_by, recorded_at, db)


def _project_channel_v2(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
    blob: bytes,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='channel',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = channel_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_channel_parity():
    recorded_by = 'peer1'
    recorded_at = 4000
    created_at = 3000

    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    signer_public_b64 = crypto.b64encode(signer_public_key)

    admin_id = 'admin_1'
    group_id = 'group_1'
    key_id = 'key_1'
    user_id = 'user_1'

    channel_name = 'general'
    disappearing_time_ms = 5000
    is_main = 1

    event_id, signed_event, blob = _build_channel_event(
        t_ms=created_at,
        name=channel_name,
        group_id=group_id,
        signed_by=signer_peer_shared_id,
        signer_private_key=signer_private_key,
        admin_grant=admin_id,
        disappearing_time_ms=disappearing_time_ms,
        is_main=is_main,
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_peer_shared(
            db,
            recorded_by,
            signer_peer_shared_id,
            signer_public_b64,
            user_id,
            created_at,
            recorded_at,
        )
        _insert_group(
            db,
            recorded_by,
            group_id,
            key_id,
            signer_peer_shared_id,
            created_at,
            recorded_at,
        )
        _insert_admin(
            db,
            recorded_by,
            admin_id,
            user_id,
            created_at,
            recorded_at,
        )
        _mark_valid(db, recorded_by, signer_peer_shared_id)
        _mark_valid(db, recorded_by, group_id)
        _mark_valid(db, recorded_by, admin_id)

    _project_channel_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_channel_v2(v2_db, recorded_by, recorded_at, event_id, signed_event, blob)

    legacy_channels = get_table_rows('channels', recorded_by, legacy_db)
    v2_channels = get_table_rows('channels', recorded_by, v2_db)

    assert len(legacy_channels) == 1
    assert len(v2_channels) == 1

    for field in (
        'channel_id',
        'name',
        'group_id',
        'signed_by',
        'created_at',
        'disappearing_time_ms',
        'is_main',
        'recorded_by',
        'recorded_at',
    ):
        assert legacy_channels[0][field] == v2_channels[0][field]
