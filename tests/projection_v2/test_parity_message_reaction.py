"""Parity tests for v2 message_reaction projectors."""
from __future__ import annotations

import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.content import message_reaction as reaction_module
from events.content import message_reaction_deletion as deletion_module

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


def _build_reaction_event(
    t_ms: int,
    message_id: str,
    reactor_id: str,
    signed_by: str,
    signer_private_key: bytes,
    global_count: int,
    emoji: str,
) -> tuple[str, dict, bytes]:
    event_data = {
        'type': 'message_reaction',
        'message_id': message_id,
        'reactor_id': reactor_id,
        'signed_by': signed_by,
        'signer_type': 'peer_shared',
        'emoji': emoji,
        'created_at': t_ms,
        'global_count': global_count,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _build_deletion_event(
    t_ms: int,
    reaction_id: str,
    deleted_by: str,
    signer_private_key: bytes,
) -> tuple[str, dict, bytes]:
    event_data = {
        'type': 'message_reaction_deletion',
        'reaction_id': reaction_id,
        'deleted_by': deleted_by,
        'signer_type': 'peer_shared',
        'created_at': t_ms,
    }
    signed_event = crypto.sign_event(event_data, signer_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_reaction_legacy(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    blob: bytes,
    event_id: str,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    reaction_module.project(event_id, recorded_by, recorded_at, db)


def _project_reaction_v2(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='message_reaction',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = reaction_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def _project_deletion_legacy(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    blob: bytes,
    event_id: str,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    deletion_module.project(event_id, recorded_by, recorded_at, db)


def _project_deletion_v2(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='message_reaction_deletion',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = deletion_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_message_reaction_parity():
    recorded_by = 'peer1'
    recorded_at = 4000
    created_at = 3000

    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    signer_public_b64 = crypto.b64encode(signer_public_key)

    message_id = 'message_1'
    channel_id = 'channel_1'
    group_id = 'group_1'
    author_id = 'user_author'
    reactor_id = 'user_reactor'

    reaction_id, signed_event, blob = _build_reaction_event(
        t_ms=created_at,
        message_id=message_id,
        reactor_id=reactor_id,
        signed_by=signer_peer_shared_id,
        signer_private_key=signer_private_key,
        global_count=5,
        emoji='👍',
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_peer_shared(
            db,
            recorded_by,
            signer_peer_shared_id,
            signer_public_b64,
            reactor_id,
            created_at,
            recorded_at,
        )
        _insert_message(
            db,
            recorded_by,
            message_id,
            channel_id,
            group_id,
            author_id,
            signer_peer_shared_id,
            created_at,
            recorded_at,
        )
        _mark_valid(db, recorded_by, signer_peer_shared_id)
        _mark_valid(db, recorded_by, message_id)

    _project_reaction_legacy(legacy_db, recorded_by, recorded_at, blob, reaction_id)
    _project_reaction_v2(v2_db, recorded_by, recorded_at, reaction_id, signed_event)

    legacy_rows = get_table_rows('message_reactions', recorded_by, legacy_db)
    v2_rows = get_table_rows('message_reactions', recorded_by, v2_db)

    assert len(legacy_rows) == 1
    assert len(v2_rows) == 1
    assert legacy_rows[0]['reaction_id'] == v2_rows[0]['reaction_id']
    assert legacy_rows[0]['message_id'] == v2_rows[0]['message_id']
    assert legacy_rows[0]['reactor_id'] == v2_rows[0]['reactor_id']
    assert legacy_rows[0]['signed_by'] == v2_rows[0]['signed_by']
    assert legacy_rows[0]['emoji'] == v2_rows[0]['emoji']
    assert legacy_rows[0]['created_at'] == v2_rows[0]['created_at']
    assert legacy_rows[0]['global_count'] == v2_rows[0]['global_count']


def test_message_reaction_deletion_parity():
    recorded_by = 'peer1'
    recorded_at = 5000
    created_at = 3500

    signer_private_key, signer_public_key = crypto.generate_keypair()
    signer_peer_shared_id = crypto.b64encode(crypto.hash(signer_public_key))
    signer_public_b64 = crypto.b64encode(signer_public_key)

    message_id = 'message_2'
    channel_id = 'channel_2'
    group_id = 'group_2'
    author_id = 'user_author_2'
    reactor_id = 'user_reactor_2'

    reaction_id, signed_event, blob = _build_reaction_event(
        t_ms=created_at,
        message_id=message_id,
        reactor_id=reactor_id,
        signed_by=signer_peer_shared_id,
        signer_private_key=signer_private_key,
        global_count=9,
        emoji='🔥',
    )

    deletion_id, deletion_event, deletion_blob = _build_deletion_event(
        t_ms=created_at + 100,
        reaction_id=reaction_id,
        deleted_by=signer_peer_shared_id,
        signer_private_key=signer_private_key,
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_peer_shared(
            db,
            recorded_by,
            signer_peer_shared_id,
            signer_public_b64,
            reactor_id,
            created_at,
            recorded_at,
        )
        _insert_message(
            db,
            recorded_by,
            message_id,
            channel_id,
            group_id,
            author_id,
            signer_peer_shared_id,
            created_at,
            recorded_at,
        )
        _mark_valid(db, recorded_by, signer_peer_shared_id)
        _mark_valid(db, recorded_by, message_id)

    _project_reaction_legacy(legacy_db, recorded_by, recorded_at, blob, reaction_id)
    _project_reaction_v2(v2_db, recorded_by, recorded_at, reaction_id, signed_event)

    _project_deletion_legacy(legacy_db, recorded_by, recorded_at + 200, deletion_blob, deletion_id)
    _project_deletion_v2(v2_db, recorded_by, recorded_at + 200, deletion_id, deletion_event)

    legacy_reactions = get_table_rows('message_reactions', recorded_by, legacy_db)
    v2_reactions = get_table_rows('message_reactions', recorded_by, v2_db)

    assert legacy_reactions == v2_reactions

    legacy_deletions = get_table_rows('message_reaction_deletions', recorded_by, legacy_db)
    v2_deletions = get_table_rows('message_reaction_deletions', recorded_by, v2_db)

    assert len(legacy_deletions) == 1
    assert len(v2_deletions) == 1
    assert legacy_deletions[0]['deletion_id'] == v2_deletions[0]['deletion_id']
    assert legacy_deletions[0]['reaction_id'] == v2_deletions[0]['reaction_id']
    assert legacy_deletions[0]['deleted_by'] == v2_deletions[0]['deleted_by']
    assert legacy_deletions[0]['created_at'] == v2_deletions[0]['created_at']
