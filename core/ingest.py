"""Fast-path ingest queue for raw transport blobs."""
from __future__ import annotations

from typing import Any, Iterable
import json

from . import crypto


def enqueue_raw_blob(blob: bytes, recorded_by: str, received_at: int, db: Any) -> None:
    """Enqueue a raw event blob without unwrap/parse for later materialization."""
    db._conn.execute(
        "INSERT INTO ingest_queue (recorded_by, received_at, blob) VALUES (?, ?, ?)",
        (recorded_by, received_at, blob),
    )


def enqueue_raw_rows(
    rows: Iterable[tuple[str, int, bytes]],
    db: Any,
    chunk_size: int = 1000,
) -> int:
    """Batch-enqueue raw blobs as (recorded_by, received_at, blob) rows."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    total = 0
    chunk: list[tuple[str, int, bytes]] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) >= chunk_size:
            db._conn.executemany(
                "INSERT INTO ingest_queue (recorded_by, received_at, blob) VALUES (?, ?, ?)",
                chunk,
            )
            total += len(chunk)
            chunk.clear()

    if chunk:
        db._conn.executemany(
            "INSERT INTO ingest_queue (recorded_by, received_at, blob) VALUES (?, ?, ?)",
            chunk,
        )
        total += len(chunk)

    return total


def materialize_ingest_queue(
    t_ms: int,
    db: Any,
    batch_size: int = 1000,
) -> list[str]:
    """Materialize queued blobs into store + recorded rows, return recorded_ids."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    cursor = db._conn.execute(
        "SELECT id, recorded_by, received_at, blob FROM ingest_queue ORDER BY id LIMIT ?",
        (batch_size,),
    )
    rows = cursor.fetchall()
    if not rows:
        return []

    event_rows: list[tuple[str, bytes, int]] = []
    recorded_rows: list[tuple[str, bytes, int]] = []
    recorded_ids: list[str] = []

    for row_id, recorded_by, received_at, blob in rows:
        stored_at = received_at or t_ms

        event_id_bytes = crypto.hash(blob)
        event_id = crypto.b64encode(event_id_bytes)
        event_rows.append((event_id, blob, stored_at))

        recorded_blob = json.dumps({
            'type': 'recorded',
            'ref_id': event_id,
            'recorded_by': recorded_by,
        }).encode('utf-8')
        recorded_id = crypto.b64encode(crypto.hash(recorded_blob))
        recorded_rows.append((recorded_id, recorded_blob, stored_at))
        recorded_ids.append(recorded_id)

    db._conn.executemany(
        "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
        event_rows,
    )
    db._conn.executemany(
        "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
        recorded_rows,
    )

    ids = [row[0] for row in rows]
    delete_chunk = 1000
    for i in range(0, len(ids), delete_chunk):
        chunk = ids[i:i + delete_chunk]
        placeholders = ",".join(["?"] * len(chunk))
        db._conn.execute(
            f"DELETE FROM ingest_queue WHERE id IN ({placeholders})",
            chunk,
        )

    return list(dict.fromkeys(recorded_ids))
