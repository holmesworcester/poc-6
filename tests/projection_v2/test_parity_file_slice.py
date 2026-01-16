"""Parity tests for v2 file_slice projector."""
from __future__ import annotations

import json
import sqlite3

from core import crypto, schema, store
from core.db import Database, create_safe_db, create_unsafe_db
from core.projection_v2 import apply as v2_apply
from core.projection_v2 import resolver as v2_resolver
from events.content import file_slice as file_slice_module

from tests.projection_v2.helpers import get_table_rows


def _new_db() -> Database:
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)
    return db


def _store_blob(db: Database, blob: bytes, t_ms: int) -> str:
    unsafedb = create_unsafe_db(db)
    return store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)


def _build_file_slice_event(
    file_id: str,
    slice_number: int,
    nonce: bytes,
    ciphertext: bytes,
    poly_tag: bytes,
    created_at: int,
) -> tuple[str, dict, bytes]:
    """Build a file_slice event (unsigned, unencrypted)."""
    event_data = {
        'type': 'file_slice',
        'file_id': file_id,
        'slice_number': slice_number,
        'nonce': crypto.b64encode(nonce),
        'ciphertext': crypto.b64encode(ciphertext),
        'poly_tag': crypto.b64encode(poly_tag),
        'created_at': created_at,
    }
    blob = crypto.canonicalize_json(event_data)
    event_id = crypto.b64encode(crypto.hash(blob))
    return event_id, event_data, blob


def _project_file_slice_legacy(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    blob: bytes,
    event_id: str,
    event_data: dict,
) -> None:
    stored_id = _store_blob(db, blob, recorded_at)
    assert stored_id == event_id
    file_slice_module.project(event_id, event_data, recorded_by, recorded_at, db)


def _project_file_slice_v2(
    db: Database,
    recorded_by: str,
    recorded_at: int,
    event_id: str,
    event_data: dict,
) -> None:
    result = v2_resolver.resolve_event(
        ref_id=event_id,
        event_type='file_slice',
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db,
    )
    assert result.status == 'ok', f"resolve failed: {result.status} {result.error}"
    projector_result = file_slice_module.project_pure(result.ctx)
    v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)


def test_file_slice_parity():
    """Test that file_slice v2 projector produces same result as legacy."""
    recorded_by = 'peer1'
    recorded_at = 1000

    file_id = crypto.b64encode(crypto.hash(b'file_seed'))
    slice_number = 0
    nonce = crypto.hash(b'nonce_0')[:24]  # 24-byte nonce
    ciphertext = b'encrypted_chunk_data_here'
    poly_tag = crypto.hash(ciphertext)[:16]

    event_id, event_data, blob = _build_file_slice_event(
        file_id=file_id,
        slice_number=slice_number,
        nonce=nonce,
        ciphertext=ciphertext,
        poly_tag=poly_tag,
        created_at=recorded_at,
    )

    legacy_db = _new_db()
    v2_db = _new_db()

    _project_file_slice_legacy(legacy_db, recorded_by, recorded_at, blob, event_id, event_data)
    _project_file_slice_v2(v2_db, recorded_by, recorded_at, event_id, event_data)

    # Compare file_slices table
    legacy_rows = get_table_rows('file_slices', recorded_by, legacy_db)
    v2_rows = get_table_rows('file_slices', recorded_by, v2_db)

    assert len(legacy_rows) == 1
    assert len(v2_rows) == 1
    assert legacy_rows[0]['file_id'] == v2_rows[0]['file_id']
    assert legacy_rows[0]['slice_number'] == v2_rows[0]['slice_number']
    assert legacy_rows[0]['nonce'] == v2_rows[0]['nonce']
    assert legacy_rows[0]['ciphertext'] == v2_rows[0]['ciphertext']
    assert legacy_rows[0]['poly_tag'] == v2_rows[0]['poly_tag']
    assert legacy_rows[0]['event_id'] == v2_rows[0]['event_id']

    # Compare event_dependencies table
    legacy_deps = get_table_rows('event_dependencies', recorded_by, legacy_db)
    v2_deps = get_table_rows('event_dependencies', recorded_by, v2_db)

    assert len(legacy_deps) == 1
    assert len(v2_deps) == 1
    assert legacy_deps[0]['child_event_id'] == v2_deps[0]['child_event_id']
    assert legacy_deps[0]['parent_event_id'] == v2_deps[0]['parent_event_id']
    assert legacy_deps[0]['dependency_type'] == v2_deps[0]['dependency_type']


def test_file_slice_multiple_slices():
    """Test projecting multiple slices for same file."""
    recorded_by = 'peer1'
    recorded_at = 1000

    file_id = crypto.b64encode(crypto.hash(b'file_seed'))

    legacy_db = _new_db()
    v2_db = _new_db()

    # Create 3 slices
    for slice_num in range(3):
        nonce = crypto.hash(f'nonce_{slice_num}'.encode())[:24]  # 24-byte nonce
        ciphertext = f'chunk_{slice_num}'.encode()
        poly_tag = crypto.hash(ciphertext)[:16]

        event_id, event_data, blob = _build_file_slice_event(
            file_id=file_id,
            slice_number=slice_num,
            nonce=nonce,
            ciphertext=ciphertext,
            poly_tag=poly_tag,
            created_at=recorded_at + slice_num,
        )

        _project_file_slice_legacy(legacy_db, recorded_by, recorded_at + slice_num, blob, event_id, event_data)
        _project_file_slice_v2(v2_db, recorded_by, recorded_at + slice_num, event_id, event_data)

    legacy_rows = get_table_rows('file_slices', recorded_by, legacy_db)
    v2_rows = get_table_rows('file_slices', recorded_by, v2_db)

    assert len(legacy_rows) == 3
    assert len(v2_rows) == 3

    # Sort by slice_number for comparison
    legacy_sorted = sorted(legacy_rows, key=lambda r: r['slice_number'])
    v2_sorted = sorted(v2_rows, key=lambda r: r['slice_number'])

    for i in range(3):
        assert legacy_sorted[i]['slice_number'] == v2_sorted[i]['slice_number']
        assert legacy_sorted[i]['file_id'] == v2_sorted[i]['file_id']
