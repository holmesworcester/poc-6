"""
Perf prototype: raw ingest queue (no unwrap/parse) + materialize to store/recorded.
Also measures hint lookup + raw log insert path.

Run with:
  SYNC_PERF_INGEST_QUEUE=1 pytest -n 0 -k perf_ingest_queue -s
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from core import crypto, ingest, schema
from core.db import Database


_DISK_TEST_DIR = Path(__file__).parent.parent / ".test_dbs"


def _make_peer_blob(seq: int, t_ms: int, target_size: int) -> bytes:
    event_data = {
        'type': 'peer',
        'public_key': crypto.b64encode((seq + 1).to_bytes(32, 'little')),
        'private_key': crypto.b64encode((seq + 2).to_bytes(32, 'little')),
        'created_at': t_ms,
        'seq': seq,
    }
    blob = json.dumps(event_data, separators=(",", ":")).encode('utf-8')
    if target_size <= 0 or len(blob) >= target_size:
        return blob

    pad_len = max(0, target_size - len(blob) - len(',"pad":""'))
    event_data['pad'] = "x" * pad_len
    blob = json.dumps(event_data, separators=(",", ":")).encode('utf-8')
    if len(blob) < target_size:
        event_data['pad'] = "x" * (pad_len + (target_size - len(blob)))
        blob = json.dumps(event_data, separators=(",", ":")).encode('utf-8')
    return blob


def _make_file_slice_blob(
    file_id: str,
    slice_number: int,
    t_ms: int,
    nonce_b64: str,
    ciphertext_b64: str,
    poly_tag_b64: str,
) -> bytes:
    event_data = {
        'type': 'file_slice',
        'file_id': file_id,
        'slice_number': slice_number,
        'nonce': nonce_b64,
        'ciphertext': ciphertext_b64,
        'poly_tag': poly_tag_b64,
        'created_at': t_ms,
    }
    return json.dumps(event_data, separators=(",", ":")).encode('utf-8')


def _iter_ingest_rows(
    start: int,
    count: int,
    small_size: int,
    file_ratio: int,
    recorded_by: str,
    t_ms: int,
    nonce_b64: str,
    ciphertext_b64: str,
    poly_tag_b64: str,
) -> list[tuple[str, int, bytes]]:
    stride = file_ratio + 1
    rows: list[tuple[str, int, bytes]] = []
    for i in range(start, start + count):
        is_small = (i - start) % stride == 0
        if is_small:
            blob = _make_peer_blob(i, t_ms, small_size)
        else:
            file_id = crypto.b64encode(i.to_bytes(16, 'little'))
            blob = _make_file_slice_blob(
                file_id,
                i,
                t_ms,
                nonce_b64,
                ciphertext_b64,
                poly_tag_b64,
            )
        rows.append((recorded_by, t_ms, blob))
    return rows


def test_perf_ingest_queue() -> None:
    if os.getenv("SYNC_PERF_INGEST_QUEUE") != "1":
        pytest.skip("Set SYNC_PERF_INGEST_QUEUE=1 to run")

    count = int(os.getenv("SYNC_PERF_INGEST_COUNT", "200000"))
    small_size = int(os.getenv("SYNC_PERF_INGEST_SMALL_SIZE", "512"))
    file_ratio = int(os.getenv("SYNC_PERF_INGEST_FILE_RATIO", "10"))
    ingest_batch = int(os.getenv("SYNC_PERF_INGEST_BATCH", "1000"))
    materialize_batch = int(os.getenv("SYNC_PERF_INGEST_MATERIALIZE_BATCH", "1000"))
    hint_batch = int(os.getenv("SYNC_PERF_INGEST_HINT_BATCH", str(ingest_batch)))
    unsafe = os.getenv("SYNC_PERF_INGEST_UNSAFE", "1") == "1"

    _DISK_TEST_DIR.mkdir(exist_ok=True)
    db_path = _DISK_TEST_DIR / f"ingest_queue_{uuid.uuid4().hex}.db"

    conn = sqlite3.connect(str(db_path))
    db = Database(conn)
    if unsafe:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = FILE")
        conn.execute("PRAGMA cache_size = -4096")
        conn.execute("PRAGMA mmap_size = 0")
        conn.execute("PRAGMA secure_delete = OFF")

    schema.create_all(db)

    recorded_by = crypto.b64encode(os.urandom(16))
    t_ms = int(time.time() * 1000)

    nonce_b64 = crypto.b64encode(os.urandom(24))
    ciphertext_b64 = crypto.b64encode(os.urandom(450))
    poly_tag_b64 = crypto.b64encode(os.urandom(16))

    file_blob_example = _make_file_slice_blob(
        crypto.b64encode((1).to_bytes(16, 'little')),
        1,
        t_ms,
        nonce_b64,
        ciphertext_b64,
        poly_tag_b64,
    )
    small_blob_example = _make_peer_blob(1, t_ms, small_size)
    file_size = len(file_blob_example)
    actual_small_size = len(small_blob_example)
    avg_size = (file_ratio * file_size + actual_small_size) / (file_ratio + 1)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_ingest_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hint BLOB NOT NULL,
            recorded_by TEXT NOT NULL,
            received_at INTEGER NOT NULL,
            source_ip TEXT,
            source_port INTEGER,
            blob BLOB NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_ingest_hint ON raw_ingest_log(hint)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingest_hint_map (
            hint BLOB PRIMARY KEY,
            recorded_by TEXT NOT NULL
        )
    """)
    hint_rows = [
        (i.to_bytes(crypto.KEY_ID_SIZE, 'little'), recorded_by)
        for i in range(128)
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO ingest_hint_map (hint, recorded_by) VALUES (?, ?)",
        hint_rows,
    )
    db.commit()

    total_hint_time = 0.0
    cursor = 0
    while cursor < count:
        batch = min(hint_batch, count - cursor)
        rows = _iter_ingest_rows(
            cursor,
            batch,
            small_size,
            file_ratio,
            recorded_by,
            t_ms,
            nonce_b64,
            ciphertext_b64,
            poly_tag_b64,
        )
        hints = [hint_rows[(cursor + i) % len(hint_rows)][0] for i in range(batch)]
        unique_hints = list({h for h in hints})
        placeholders = ",".join(["?"] * len(unique_hints))
        start = time.perf_counter()
        hint_map = {}
        if unique_hints:
            mapped = conn.execute(
                f"SELECT hint, recorded_by FROM ingest_hint_map WHERE hint IN ({placeholders})",
                unique_hints,
            ).fetchall()
            hint_map = {row[0]: row[1] for row in mapped}
        log_rows = []
        for i in range(batch):
            hint = hints[i]
            mapped_peer = hint_map.get(hint, recorded_by)
            log_rows.append(
                (hint, mapped_peer, t_ms, "127.0.0.1", 12345, rows[i][2])
            )
        conn.executemany(
            "INSERT INTO raw_ingest_log (hint, recorded_by, received_at, source_ip, source_port, blob) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            log_rows,
        )
        total_hint_time += time.perf_counter() - start
        cursor += batch
    db.commit()

    t_ingest = 0.0
    cursor = 0
    while cursor < count:
        batch = min(ingest_batch, count - cursor)
        rows = _iter_ingest_rows(
            cursor,
            batch,
            small_size,
            file_ratio,
            recorded_by,
            t_ms,
            nonce_b64,
            ciphertext_b64,
            poly_tag_b64,
        )
        start = time.perf_counter()
        ingest.enqueue_raw_rows(rows, db, chunk_size=ingest_batch)
        t_ingest += time.perf_counter() - start
        cursor += batch
    db.commit()

    start = time.perf_counter()
    total_recorded = 0
    while True:
        recorded_ids = ingest.materialize_ingest_queue(
            t_ms,
            db,
            batch_size=materialize_batch,
        )
        if not recorded_ids:
            break
        total_recorded += len(recorded_ids)
    db.commit()
    t_materialize = time.perf_counter() - start

    mb = (count * avg_size) / (1024 * 1024)
    print(
        f"mix: {file_ratio}:1 files:small "
        f"(file={file_size}B, small={actual_small_size}B, avg={avg_size:.1f}B)"
    )
    print(
        f"ingest-queue: {count} blobs in {t_ingest:.3f}s "
        f"({count / t_ingest:.1f} blobs/s, {mb / t_ingest:.1f} MB/s)"
    )
    print(
        f"hint+rawlog: {count} blobs in {total_hint_time:.3f}s "
        f"({count / total_hint_time:.1f} blobs/s, {mb / total_hint_time:.1f} MB/s)"
    )
    print(
        f"materialize: {total_recorded} recorded in {t_materialize:.3f}s "
        f"({total_recorded / t_materialize:.1f} rec/s, {mb / t_materialize:.1f} MB/s)"
    )

    assert t_ingest > 0 and t_materialize > 0 and total_hint_time > 0
