"""Parity tests for v2 message projector."""
from __future__ import annotations

import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.content import message as message_module

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


def _insert_user(
    db: Database,
    recorded_by: str,
    user_id: str,
    name: str,
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
        (
            user_id,
            name,
            network_id,
            created_at,
            user_pubkey_b64,
            recorded_by,
            recorded_at,
        ),
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
        (
            channel_id,
            'general',
            group_id,
            signed_by,
            created_at,
            0,
            0,
            recorded_by,
            recorded_at,
        ),
    )


def _build_message_event(
    t_ms: int,
    channel_id: str,
    author_id: str,
    signed_by: str,
    signer_private_key: bytes,
    content: str,
    disappearing_time_ms: int,
) -> tuple[str, dict, bytes]:
    event_data = {
        'type': 'message',
        'channel_id': channel_id,
        'author_id': author_id,
        'signed_by': signed_by,
        'signer_type': 'peer_shared',
        'content': content,
        'created_at': t_ms,
        'disappearing_time_ms': disappearing_time_ms,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_message_legacy(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    blob: bytes,
    event_id: str,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    message_module.project(event_id, recorded_by, recorded_at, db)


def _project_message_v2(
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
        event_type='message',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = message_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_message_parity():
    recorded_by = 'peer1'
    recorded_at = 4000
    created_at = 3000

    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    signer_public_b64 = crypto.b64encode(signer_public_key)

    _, user_public_key = crypto.generate_keypair()
    user_public_b64 = crypto.b64encode(user_public_key)

    channel_id = 'channel_1'
    group_id = 'group_1'
    author_id = 'user_author'
    network_id = 'network_1'
    content = 'hello world'
    disappearing_time_ms = 5000

    event_id, signed_event, blob = _build_message_event(
        t_ms=created_at,
        channel_id=channel_id,
        author_id=author_id,
        signed_by=signer_peer_shared_id,
        signer_private_key=signer_private_key,
        content=content,
        disappearing_time_ms=disappearing_time_ms,
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_peer_shared(
            db,
            recorded_by,
            signer_peer_shared_id,
            signer_public_b64,
            author_id,
            created_at,
            recorded_at,
        )
        _insert_user(
            db,
            recorded_by,
            author_id,
            'Author',
            network_id,
            user_public_b64,
            created_at,
            recorded_at,
        )
        _insert_channel(
            db,
            recorded_by,
            channel_id,
            group_id,
            signer_peer_shared_id,
            created_at,
            recorded_at,
        )
        _mark_valid(db, recorded_by, signer_peer_shared_id)
        _mark_valid(db, recorded_by, author_id)
        _mark_valid(db, recorded_by, channel_id)

    _project_message_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_message_v2(v2_db, recorded_by, recorded_at, event_id, signed_event, blob)

    legacy_messages = get_table_rows('messages', recorded_by, legacy_db)
    v2_messages = get_table_rows('messages', recorded_by, v2_db)

    assert len(legacy_messages) == 1
    assert len(v2_messages) == 1

    for field in (
        'message_id',
        'channel_id',
        'group_id',
        'author_id',
        'signed_by',
        'content',
        'created_at',
        'ttl_ms',
        'key_id',
        'recorded_by',
        'recorded_at',
    ):
        assert legacy_messages[0][field] == v2_messages[0][field]

    legacy_deps = get_table_rows('event_dependencies', recorded_by, legacy_db)
    v2_deps = get_table_rows('event_dependencies', recorded_by, v2_db)

    assert len(legacy_deps) == 1
    assert len(v2_deps) == 1

    for field in ('child_event_id', 'parent_event_id', 'dependency_type', 'recorded_by'):
        assert legacy_deps[0][field] == v2_deps[0][field]
