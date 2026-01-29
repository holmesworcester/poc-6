"""Fast-path ingest helpers for transport blobs."""
from __future__ import annotations

from typing import Any, Iterable
import json
import logging

from core import crypto
from core import wire_format
from core import transport
from events import registry
from events.network import negentropy
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


def _unwrap_transit_with_key(blob: bytes, key_data: dict[str, Any]) -> bytes | None:
    if not key_data:
        return None
    id_bytes = blob[:crypto.KEY_ID_SIZE]
    encrypted_data = blob[crypto.KEY_ID_SIZE:]

    try:
        if key_data.get('type') == 'symmetric':
            nonce = encrypted_data[:crypto.NONCE_SIZE]
            ciphertext = encrypted_data[crypto.NONCE_SIZE:]
            return crypto.decrypt(ciphertext, key_data['key'], nonce)
        if key_data.get('type') == 'asymmetric':
            return crypto.unseal(encrypted_data, key_data['private_key'])
    except Exception:
        return None
    return None


def _send_transport_ack(db: Any, key_id: str, ack_count: int, t_ms: int) -> bool:
    """Send a transport ACK to a connection.

    Args:
        db: Database connection
        key_id: Connection key_id to send ACK on
        ack_count: Number of blobs being acknowledged
        t_ms: Current timestamp

    Returns:
        True if sent successfully
    """
    if ack_count <= 0:
        return False

    # Get recorded_by for this connection
    row = db._conn.execute(
        "SELECT recorded_by FROM connections WHERE key_id = ?",
        (key_id,),
    ).fetchone()

    if not row:
        log.debug(f"_send_transport_ack: connection {key_id[:16]}... not found")
        return False

    recorded_by = row[0]

    # Encode the transport_ack event
    ack_blob = wire_format.encode_transport_ack_wire_event(
        ack_count=ack_count,
        created_at_ms=t_ms,
    )

    # Use connection_request.send which handles all the address resolution
    from events.network import connection_request
    result = connection_request.send(recorded_by, key_id, ack_blob, t_ms, db)

    if result:
        log.debug(f"_send_transport_ack: sent ack_count={ack_count} to {key_id[:16]}...")
    return result


def queue_incoming(
    batch: Iterable[tuple[bytes, tuple[str, int] | None]],
    t_ms: int,
    db: Any,
    chunk_size: int = 1000,
) -> int:
    """Route and append incoming blobs to the ingest log.

    We batch-resolve key hints and store unwrapped event blobs with
    transit_wrapped=0.
    """
    rows: list[tuple[bytes, str, int, str | None, int | None, str | None, bytes, int]] = []
    entries: list[tuple[bytes, tuple[str, int] | None, bytes, str]] = []
    key_ids: set[str] = set()

    for blob, from_addr in batch:
        if not blob:
            continue
        key_id_bytes = blob[:crypto.KEY_ID_SIZE] if len(blob) >= crypto.KEY_ID_SIZE else b""
        if not key_id_bytes:
            continue
        key_id_b64 = crypto.b64encode(key_id_bytes)
        entries.append((blob, from_addr, key_id_bytes, key_id_b64))
        key_ids.add(key_id_b64)

    if key_ids:
        placeholders = ",".join("?" for _ in key_ids)
        params = tuple(key_ids)

        key_map: dict[str, list[tuple[str, dict[str, Any]]]] = {}

        conn_rows = db._conn.execute(
            f"SELECT key_id, recorded_by, our_key FROM connections WHERE key_id IN ({placeholders})",
            params,
        ).fetchall()
        for key_id, recorded_by, our_key in conn_rows:
            key_data = {
                'id': crypto.b64decode(key_id),
                'key': our_key,
                'type': 'symmetric',
            }
            key_map.setdefault(key_id, []).append((recorded_by, key_data))

        prekey_rows = db._conn.execute(
            f"SELECT connection_prekey_id, owner_peer_id, private_key "
            f"FROM connection_prekeys WHERE connection_prekey_id IN ({placeholders})",
            params,
        ).fetchall()
        for key_id, owner_peer_id, private_key in prekey_rows:
            key_data = {
                'id': crypto.b64decode(key_id),
                'private_key': private_key,
                'type': 'asymmetric',
            }
            key_map.setdefault(key_id, []).append((owner_peer_id, key_data))

        peer_rows = db._conn.execute(
            f"SELECT peer_id, private_key FROM local_peers WHERE peer_id IN ({placeholders})",
            params,
        ).fetchall()
        for peer_id, private_key in peer_rows:
            key_data = {
                'id': crypto.b64decode(peer_id),
                'private_key': private_key,
                'type': 'asymmetric',
            }
            key_map.setdefault(peer_id, []).append((peer_id, key_data))

        # Track receipts per flow_key (connection key_id) for ACKs
        ack_pending: dict[str, int] = {}  # flow_key -> ack_count

        for blob, from_addr, _key_id_bytes, key_id_b64 in entries:
            candidates = key_map.get(key_id_b64, [])
            if not candidates:
                continue
            source_ip, source_port = (from_addr if from_addr else (None, None))
            for recorded_by, key_data in candidates:
                event_blob = _unwrap_transit_with_key(blob, key_data)
                if not event_blob:
                    continue

                # Handle transport_ack immediately - don't queue
                if wire_format.is_wire_transport_ack_envelope(event_blob):
                    try:
                        ack_data = wire_format.decode_transport_ack_wire_event(event_blob)
                        transport.flow_on_ack(key_id_b64, ack_data.get("ack_count", 0))
                    except Exception as e:
                        log.warning(f"ingest: failed to decode transport_ack: {e}")
                    continue  # Don't add to rows

                # Track receipt for flow control ACKs
                ack_count = transport.flow_on_receive(key_id_b64, t_ms)
                if ack_count > 0:
                    ack_pending[key_id_b64] = ack_pending.get(key_id_b64, 0) + ack_count

                hint = (
                    b""
                    if event_blob[:1] in (b"{", b"[")
                    else event_blob[:crypto.KEY_ID_SIZE] if len(event_blob) >= crypto.KEY_ID_SIZE else b""
                )
                rows.append((hint, recorded_by, t_ms, source_ip, source_port, None, event_blob, 0))

        # Send ACKs for flows that reached threshold
        for flow_key, ack_count in ack_pending.items():
            _send_transport_ack(db, flow_key, ack_count, t_ms)

    if not rows:
        return 0

    return append_incoming_log(rows, db, chunk_size=chunk_size)


def append_incoming_log(
    rows: Iterable[tuple[bytes, str, int, str | None, int | None, str | None, bytes, int]],
    db: Any,
    chunk_size: int = 1000,
) -> int:
    """Append incoming blobs into incoming_event_log.

    Args:
        rows: Iterable of (hint, recorded_by, received_at, source_ip, source_port, event_type, blob, transit_wrapped)
        db: Database connection
        chunk_size: Batch size for executemany

    Returns:
        Number of rows inserted.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    total = 0
    batch: list[tuple[bytes, str, int, str | None, int | None, str | None, bytes, int]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= chunk_size:
            db._conn.executemany(
                "INSERT INTO incoming_event_log "
                "(hint, recorded_by, received_at, source_ip, source_port, event_type, blob, transit_wrapped) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            total += len(batch)
            batch.clear()

    if batch:
        db._conn.executemany(
            "INSERT INTO incoming_event_log "
            "(hint, recorded_by, received_at, source_ip, source_port, event_type, blob, transit_wrapped) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
        total += len(batch)

    return total


def _event_type_from_envelope(event_blob: bytes) -> str | None:
    """Get event type from wire envelope using registry."""
    type_code = wire_format.get_wire_type_code(event_blob)
    if type_code is not None:
        return registry.get_event_type_by_code(type_code)
    return None


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
        "SELECT id, hint, recorded_by, received_at, source_ip, source_port, blob, event_type, transit_wrapped "
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

    for log_id, _transit_hint, recorded_by, received_at, source_ip, source_port, blob, event_type, transit_wrapped in rows:
        stored_at = received_at or t_ms
        if transit_wrapped:
            event_blob, _missing = crypto.unwrap_transit(blob, recorded_by, db)
            if not event_blob:
                log.warning("materialize_log_batch: transit unwrap failed for log_id=%s", log_id)
                continue
        else:
            event_blob = blob

        if event_blob[:1] in (b'{', b'['):
            raise ValueError("JSON event blobs are no longer supported")

        if negentropy.is_wire_envelope(event_blob):
            event_data = negentropy.decode_wire_event(event_blob)
            protocol_rows.append((log_id, recorded_by, event_data))
            protocol_log_ids.append(log_id)
            continue

        event_id = crypto.b64encode(crypto.hash(event_blob))
        event_rows.append((event_id, event_blob, stored_at))

        event_hint = event_blob[:crypto.KEY_ID_SIZE] if len(event_blob) >= crypto.KEY_ID_SIZE else b''
        event_type = _event_type_from_envelope(event_blob)

        recorded_blob = json.dumps({
            'type': 'recorded',
            'ref_id': event_id,
            'recorded_by': recorded_by,
        }).encode('utf-8')
        recorded_id = crypto.b64encode(crypto.hash(recorded_blob))
        recorded_rows.append((recorded_id, recorded_blob, stored_at))
        recorded_ids.append(recorded_id)
        index_rows.append((log_id, event_id, recorded_id, recorded_by, event_hint, event_type, stored_at))

        if event_type:
            is_shareable = registry.is_shareable(event_type)
        else:
            is_shareable = True  # Opaque events are assumed shareable

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
        for recorded_by, peer_rows in by_peer.items():
            safedb = create_safe_db(db, recorded_by=recorded_by)
            safedb.executemany(
                "INSERT OR REPLACE INTO packet_metadata "
                "(event_id, recorded_by, from_addr_ip, from_addr_port) VALUES (?, ?, ?, ?)",
                peer_rows,
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
