"""Fast-path ingest helpers for raw transport blobs."""
from __future__ import annotations

from typing import Any, Iterable
import json
import logging

from core import crypto
from events import registry
from core.db import create_safe_db

log = logging.getLogger(__name__)

def get_stream_cursor(db: Any, stream_name: str) -> int:
    """Get last processed log id for a stream."""
    row = db._conn.execute(
        "SELECT last_log_id FROM projection_streams WHERE stream_name = ?",
        (stream_name,),
    ).fetchone()
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def get_log_tail_id(db: Any) -> int:
    """Get the latest incoming_event_log id (0 if empty)."""
    row = db._conn.execute("SELECT MAX(id) FROM incoming_event_log").fetchone()
    return row[0] if row and row[0] is not None else 0


def set_stream_cursor(db: Any, stream_name: str, last_log_id: int, t_ms: int) -> None:
    """Persist stream cursor after a successful batch."""
    db._conn.execute(
        "INSERT OR REPLACE INTO projection_streams (stream_name, last_log_id, updated_at) "
        "VALUES (?, ?, ?)",
        (stream_name, last_log_id, t_ms),
    )


def route_blob_to_peers(blob: bytes, db: Any) -> list[str]:
    """Device-wide routing: determine which local peers can decrypt this blob."""
    key_id = blob[:crypto.KEY_ID_SIZE]
    key_id_b64 = crypto.b64encode(key_id)

    try:
        cursor = db._conn.execute(
            "SELECT DISTINCT recorded_by FROM connections WHERE key_id = ?",
            (key_id_b64,),
        )
        recorded_by_peers = [row[0] for row in cursor.fetchall()]
        if recorded_by_peers:
            return recorded_by_peers
    except Exception as e:
        log.warning("route_blob_to_peers: Failed to query connections: %s", e)

    try:
        cursor = db._conn.execute(
            "SELECT DISTINCT owner_peer_id FROM connection_prekeys WHERE connection_prekey_id = ?",
            (key_id_b64,),
        )
        recorded_by_peers = [row[0] for row in cursor.fetchall()]
        if recorded_by_peers:
            return recorded_by_peers
    except Exception as e:
        log.warning("route_blob_to_peers: Failed to query connection_prekeys: %s", e)

    return []


def queue_incoming(
    batch: Iterable[tuple[bytes, tuple[str, int] | None]],
    t_ms: int,
    db: Any,
    chunk_size: int = 1000,
) -> int:
    """Route and append incoming transport blobs to the ingest log."""
    rows: list[tuple[bytes, str, int, str | None, int | None, str | None, bytes]] = []
    for blob, from_addr in batch:
        recorded_by_peers = route_blob_to_peers(blob, db)
        if not recorded_by_peers:
            continue
        hint = blob[:crypto.KEY_ID_SIZE] if len(blob) >= crypto.KEY_ID_SIZE else b""
        source_ip, source_port = (from_addr if from_addr else (None, None))
        for peer_id in recorded_by_peers:
            rows.append((hint, peer_id, t_ms, source_ip, source_port, None, blob))

    if not rows:
        return 0

    return append_incoming_log(rows, db, chunk_size=chunk_size)


def append_incoming_log(
    rows: Iterable[tuple[bytes, str, int, str | None, int | None, str | None, bytes]],
    db: Any,
    chunk_size: int = 1000,
) -> int:
    """Append raw incoming blobs into incoming_event_log.

    Args:
        rows: Iterable of (hint, recorded_by, received_at, source_ip, source_port, event_type, blob)
        db: Database connection
        chunk_size: Batch size for executemany

    Returns:
        Number of rows inserted.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    total = 0
    batch: list[tuple[bytes, str, int, str | None, int | None, str | None, bytes]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= chunk_size:
            db._conn.executemany(
                "INSERT INTO incoming_event_log "
                "(hint, recorded_by, received_at, source_ip, source_port, event_type, blob) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            total += len(batch)
            batch.clear()

    if batch:
        db._conn.executemany(
            "INSERT INTO incoming_event_log "
            "(hint, recorded_by, received_at, source_ip, source_port, event_type, blob) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
        total += len(batch)

    return total


def materialize_log_batch(
    db: Any,
    start_log_id: int,
    limit: int,
    t_ms: int,
) -> tuple[list[str], int, list[str], list[tuple[str, str, int]], list[tuple[int, str, dict]]]:
    """Materialize incoming_event_log rows into store/recorded and ingest_index.

    Returns:
        (recorded_ids, max_log_id, plaintext_recorded_ids, shareable_rows, protocol_rows)
    """
    if limit < 1:
        return ([], start_log_id, [], [], [])

    cursor = db._conn.execute(
        "SELECT id, hint, recorded_by, received_at, source_ip, source_port, blob, event_type "
        "FROM incoming_event_log WHERE id > ? AND (event_type IS NULL OR event_type != 'negentropy') "
        "ORDER BY id LIMIT ?",
        (start_log_id, limit),
    )
    rows = cursor.fetchall()
    if not rows:
        return ([], start_log_id, [], [], [])

    event_rows: list[tuple[str, bytes, int]] = []
    recorded_rows: list[tuple[str, bytes, int]] = []
    index_rows: list[tuple[int, str, str, str, bytes, str | None, int]] = []
    recorded_ids: list[str] = []
    plaintext_recorded_ids: list[str] = []
    shareable_rows: list[tuple[str, str, int]] = []
    protocol_rows: list[tuple[int, str, dict]] = []
    metadata_rows: list[tuple[str, str, str | None, int | None]] = []
    protocol_log_ids: list[int] = []

    for log_id, _transit_hint, recorded_by, received_at, source_ip, source_port, blob, event_type in rows:
        stored_at = received_at or t_ms
        event_blob, _missing = crypto.unwrap_transit(blob, recorded_by, db)
        if not event_blob:
            log.warning("materialize_log_batch: transit unwrap failed for log_id=%s", log_id)
            continue

        event_data = None
        is_plaintext = False
        if event_blob[:1] in (b'{', b'['):
            try:
                event_data = crypto.parse_json(event_blob)
                is_plaintext = True
            except Exception:
                is_plaintext = False

        if is_plaintext and isinstance(event_data, dict) and event_data.get("type") == "negentropy":
            protocol_rows.append((log_id, recorded_by, event_data))
            protocol_log_ids.append(log_id)
            continue

        event_id = crypto.b64encode(crypto.hash(event_blob))
        event_rows.append((event_id, event_blob, stored_at))

        if is_plaintext:
            event_hint = b''
            event_type = event_data.get('type') if isinstance(event_data, dict) else None
        else:
            event_hint = event_blob[:crypto.KEY_ID_SIZE] if len(event_blob) >= crypto.KEY_ID_SIZE else b''
            event_type = None

        recorded_blob = json.dumps({
            'type': 'recorded',
            'ref_id': event_id,
            'recorded_by': recorded_by,
        }).encode('utf-8')
        recorded_id = crypto.b64encode(crypto.hash(recorded_blob))
        recorded_rows.append((recorded_id, recorded_blob, stored_at))
        recorded_ids.append(recorded_id)
        index_rows.append((log_id, event_id, recorded_id, recorded_by, event_hint, event_type, stored_at))

        if is_plaintext:
            plaintext_recorded_ids.append(recorded_id)
            event_type = event_data.get('type') if isinstance(event_data, dict) else None
            is_shareable = bool(event_type and registry.is_shareable(event_type))
        else:
            is_shareable = True  # Encrypted or opaque events are assumed shareable

        if is_shareable:
            shareable_rows.append((event_id, recorded_by, stored_at))

        if source_ip or source_port:
            metadata_rows.append((event_id, recorded_by, source_ip, source_port))

    db._conn.executemany(
        "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
        event_rows,
    )
    db._conn.executemany(
        "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
        recorded_rows,
    )
    db._conn.executemany(
        "INSERT OR IGNORE INTO ingest_index "
        "(log_id, event_id, recorded_id, recorded_by, hint, event_type, received_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        index_rows,
    )
    if metadata_rows:
        by_peer: dict[str, list[tuple[str, str, str | None, int | None]]] = {}
        for event_id, recorded_by, source_ip, source_port in metadata_rows:
            by_peer.setdefault(recorded_by, []).append(
                (event_id, recorded_by, source_ip, source_port)
            )
        for recorded_by, rows in by_peer.items():
            safedb = create_safe_db(db, recorded_by=recorded_by)
            safedb.executemany(
                "INSERT OR REPLACE INTO packet_metadata "
                "(event_id, recorded_by, from_addr_ip, from_addr_port) VALUES (?, ?, ?, ?)",
                rows,
            )

    if protocol_log_ids:
        db._conn.executemany(
            "UPDATE incoming_event_log SET event_type = 'negentropy' WHERE id = ?",
            [(log_id,) for log_id in protocol_log_ids],
        )

    try:
        max_log_id = int(rows[-1][0])
    except (TypeError, ValueError):
        max_log_id = start_log_id
    return (list(dict.fromkeys(recorded_ids)), max_log_id, plaintext_recorded_ids, shareable_rows, protocol_rows)
