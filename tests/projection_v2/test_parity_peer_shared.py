"""Parity tests for v2 peer_shared projector."""
from __future__ import annotations

import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.identity import peer_shared as peer_shared_module

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


def _insert_invite(
    db: Database,
    recorded_by: str,
    invite_id: str,
    invite_pubkey_b64: str,
    user_id: str,
    created_at: int,
) -> None:
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO invites
           (invite_id, invite_pubkey, group_id, inviter_id, mode, user_id, created_at, recorded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            invite_id,
            invite_pubkey_b64,
            'group_1',
            'inviter_1',
            'peer',
            user_id,
            created_at,
            recorded_by,
        ),
    )


def _build_peer_shared_event(
    t_ms: int,
    peer_id: str,
    public_key_b64: str,
    invite_id: str,
    invite_private_key: bytes,
) -> tuple[str, dict, bytes]:
    event_data = {
        'type': 'peer_shared',
        'public_key': public_key_b64,
        'peer_id': peer_id,
        'created_at': t_ms,
        'invite_id': invite_id,
        'signed_by': invite_id,
        'signer_type': 'invite',
    }
    signed_event = crypto.sign_event(event_data, invite_private_key)
    blob = crypto.canonicalize_json(signed_event)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, signed_event, blob


def _project_peer_shared_legacy(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    blob: bytes,
    event_id: str,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    peer_shared_module.project(event_id, recorded_by, recorded_at, db)


def _project_peer_shared_v2(
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
        event_type='peer_shared',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = peer_shared_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_peer_shared_parity():
    recorded_by = 'peer_local'
    recorded_at = 4000
    created_at = 3000

    invite_private_key, invite_public_key = crypto.generate_keypair()
    invite_pubkey_b64 = crypto.b64encode(invite_public_key)
    invite_id = 'invite_1'
    user_id = 'user_1'

    _, peer_public_key = crypto.generate_keypair()
    public_key_b64 = crypto.b64encode(peer_public_key)

    peer_id = recorded_by

    event_id, signed_event, blob = _build_peer_shared_event(
        t_ms=created_at,
        peer_id=peer_id,
        public_key_b64=public_key_b64,
        invite_id=invite_id,
        invite_private_key=invite_private_key,
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    for db in (legacy_db, v2_db):
        _insert_invite(db, recorded_by, invite_id, invite_pubkey_b64, user_id, created_at)
        _mark_valid(db, recorded_by, invite_id)

    _project_peer_shared_legacy(legacy_db, recorded_by, recorded_at, blob, event_id)
    _project_peer_shared_v2(v2_db, recorded_by, recorded_at, event_id, signed_event, blob)

    legacy_peers = get_table_rows('peers_shared', recorded_by, legacy_db)
    v2_peers = get_table_rows('peers_shared', recorded_by, v2_db)

    assert len(legacy_peers) == 1
    assert len(v2_peers) == 1

    for field in (
        'peer_shared_id',
        'peer_id',
        'public_key',
        'user_id',
        'device_name',
        'created_at',
        'recorded_by',
        'recorded_at',
    ):
        assert legacy_peers[0][field] == v2_peers[0][field]

    legacy_self = get_table_rows('peer_self', recorded_by, legacy_db)
    v2_self = get_table_rows('peer_self', recorded_by, v2_db)

    assert len(legacy_self) == 1
    assert len(v2_self) == 1

    for field in (
        'peer_id',
        'peer_shared_id',
        'user_id',
        'recorded_by',
        'recorded_at',
    ):
        assert legacy_self[0][field] == v2_self[0][field]
