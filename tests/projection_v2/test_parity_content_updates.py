"""Parity tests for v2 content update projectors."""
from __future__ import annotations

import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.content import channel_update as channel_update_module
from events.content import message_update as message_update_module

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
           (peer_shared_id, peer_id, public_key, user_id, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            peer_shared_id,
            recorded_by,
            public_key_b64,
            user_id,
            created_at,
            recorded_by,
            recorded_at,
        ),
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


def _insert_channel(
    db: Database,
    recorded_by: str,
    channel_id: str,
    group_id: str,
    signed_by: str,
    created_at: int,
    recorded_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO channels
           (channel_id, name, group_id, signed_by, created_at, disappearing_time_ms, is_main, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (channel_id, 'general', group_id, signed_by, created_at, 0, 0, recorded_by, recorded_at),
    )


def _insert_message(
    db: Database,
    recorded_by: str,
    message_id: str,
    channel_id: str,
    group_id: str,
    author_id: str,
    signed_by: str,
    created_at: int,
    recorded_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, channel_id, group_id, author_id, signed_by, content, created_at,
            ttl_ms, key_id, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            channel_id,
            group_id,
            author_id,
            signed_by,
            'hello',
            created_at,
            0,
            None,
            recorded_by,
            recorded_at,
        ),
    )


def _build_channel_update_event(
    t_ms: int,
    channel_id: str,
    group_id: str,
    updated_by: str,
    signer_private_key: bytes,
    global_count: int,
    new_channel_name: str | None,
    new_disappearing_time_ms: int | None,
) -> tuple[str, dict, bytes]:
    event_data = {
        'type': 'channel_update',
        'channel_id': channel_id,
        'group_id': group_id,
        'updated_by': updated_by,
        'signer_type': 'peer_shared',
        'created_at': t_ms,
        'global_count': global_count,
        'new_channel_name': new_channel_name,
        'new_disappearing_time_ms': new_disappearing_time_ms,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _build_message_update_event(
    t_ms: int,
    message_id: str,
    group_id: str,
    edited_by: str,
    author_id: str,
    signer_private_key: bytes,
    global_count: int,
    new_content: str,
) -> tuple[str, dict, bytes]:
    event_data = {
        'type': 'message_update',
        'message_id': message_id,
        'group_id': group_id,
        'edited_by': edited_by,
        'signer_type': 'peer_shared',
        'author_id': author_id,
        'global_count': global_count,
        'new_content': new_content,
        'created_at': t_ms,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_channel_update_legacy(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    blob: bytes,
    event_id: str,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    channel_update_module.project(event_id, recorded_by, recorded_at, db)


def _project_channel_update_v2(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='channel_update',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = channel_update_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def _project_message_update_legacy(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    blob: bytes,
    event_id: str,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    message_update_module.project(event_id, recorded_by, recorded_at, db)


def _project_message_update_v2(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='message_update',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = message_update_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_channel_update_parity():
    recorded_by = 'peer1'
    recorded_at = 5000

    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    signer_user_id = 'user_1'
    network_id = 'network_1'
    channel_id = 'channel_1'
    group_id = 'group_1'

    event_id, signed_event, blob = _build_channel_update_event(
        t_ms=recorded_at,
        channel_id=channel_id,
        group_id=group_id,
        updated_by=signer_peer_shared_id,
        signer_private_key=signer_private_key,
        global_count=1,
        new_channel_name='new-name',
        new_disappearing_time_ms=None,
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_network(db, recorded_by, network_id, crypto.b64encode(signer_public_key), 1000, recorded_at)
        _insert_peer_shared(db, recorded_by, signer_peer_shared_id, crypto.b64encode(signer_public_key), signer_user_id, 1000, recorded_at)
        _insert_admin(db, recorded_by, 'admin_1', network_id, signer_user_id, network_id, 1000, recorded_at)
        _insert_channel(db, recorded_by, channel_id, group_id, signer_peer_shared_id, 1000, recorded_at)
        _mark_valid(db, recorded_by, signer_peer_shared_id)
        _mark_valid(db, recorded_by, channel_id)

    _project_channel_update_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_channel_update_v2(v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('channel_updates', recorded_by, legacy_db)
    v2_rows = get_table_rows('channel_updates', recorded_by, v2_db)

    assert legacy_rows == v2_rows


def test_message_update_parity():
    recorded_by = 'peer1'
    recorded_at = 7000

    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    signer_user_id = 'user_2'
    message_id = 'message_1'
    channel_id = 'channel_2'
    group_id = 'group_2'

    event_id, signed_event, blob = _build_message_update_event(
        t_ms=recorded_at,
        message_id=message_id,
        group_id=group_id,
        edited_by=signer_peer_shared_id,
        author_id=signer_user_id,
        signer_private_key=signer_private_key,
        global_count=1,
        new_content='updated',
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_peer_shared(db, recorded_by, signer_peer_shared_id, crypto.b64encode(signer_public_key), signer_user_id, 1000, recorded_at)
        _insert_message(db, recorded_by, message_id, channel_id, group_id, signer_user_id, signer_peer_shared_id, 1000, recorded_at)
        _mark_valid(db, recorded_by, signer_peer_shared_id)
        _mark_valid(db, recorded_by, message_id)

    _project_message_update_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_message_update_v2(v2_db, recorded_by, recorded_at, event_id, signed_event)

    legacy_rows = get_table_rows('message_updates', recorded_by, legacy_db)
    v2_rows = get_table_rows('message_updates', recorded_by, v2_db)

    assert legacy_rows == v2_rows
