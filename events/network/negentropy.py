"""
Negentropy-style deterministic sync protocol - UNIFIED TIME+HASH variant.

Uses requester-driven bisection with a unified time+hash key:
- unified_key = (timestamp_ms << 16) | hash_16bits = 64-bit integer
- Ranges are arbitrary [start, end) intervals, not fixed buckets
- Requester controls bisection: on mismatch, split range in half
- Responder is stateless: just compares hashes and sends blobs

Design goals:
- Deterministic: no false positives, exact set reconciliation
- Finality: clear "done" state when synced (root hashes match)
- Temporal locality: time is high-order bits, recent events cluster
- Requester-driven: responder is stateless, requester controls subdivision

Protocol flow:
1. On connection: requester sends range_request(0, MAX, hash)
2. Responder compares hashes:
   - Match → range_matched
   - Mismatch + few events → send blobs, return range_matched
   - Mismatch + many events → range_mismatched
3. On range_mismatched: requester bisects and sends two child requests
4. Continue until all ranges matched
5. When root hashes match: sync complete
"""

import hashlib
import logging
import os
import sqlite3
import struct
from datetime import datetime, timezone
from typing import Optional, Any
from dataclasses import dataclass
from enum import Enum

from core.db import create_safe_db
from core import crypto
from core import wire_format
from core import store
from core.projection.types import ProjectorResult, Command
from core.projection.apply import register_command_handler
from events.network import sync_window

log = logging.getLogger(__name__)


# Registry metadata
EVENT_TYPE = 'negentropy'
SHAREABLE = False  # Point-to-point sync protocol, don't broadcast to others
PROJECTION_TABLE = None  # No persistent projection table

# Wire format constants
WIRE_TYPE_CODE = 0x37  # TYPE_NEGENTROPY
WIRE_PLAINTEXT_SIZE = 344  # NEGENTROPY_PLAINTEXT_SIZE

# Negentropy message types
MSG_RANGE_REQUEST = 1
MSG_RANGE_MATCHED = 2
MSG_RANGE_MISMATCHED = 3
MSG_RANGE_GET = 4    # GET(range_id, start, end, cursor, limit) - cursor-based streaming
MSG_RANGE_PAGE = 5   # PAGE(range_id, items_count, cursor, has_more) - response to GET

# Range constants (use signed 64-bit range for SQLite compatibility)
MIN_UNIFIED_KEY = 0
MAX_UNIFIED_KEY = (1 << 63) - 1  # Max signed 64-bit integer

# Threshold for sending blobs vs bisecting
EVENTS_THRESHOLD = 50

# Batch size for bulk transfer (when their_hash is ZERO_HASH)
# Increased from 100 to 1000 for cursor-based streaming to reduce round-trips
BULK_BATCH_SIZE = 1000

# Chunk size for precomputed fingerprints (must be power of 2)
# 4096 chunks give ~16 million events per chunk at 48-bit timestamp resolution
CHUNK_SIZE = 4096

# Wire format sizes
RANGE_ID_SIZE = 8
EVENT_ID_MAX = 15

# v2 event specification - minimal, as this is a sync protocol message
EVENT_SPEC = {
    'encrypted': False,  # Plain JSON sync protocol message
    'signer': None,  # No signature verification
    'requires': {},  # No dependencies
    'optional': {},
    'cascade_on_delete': [],
}


# Wire format functions - encode/decode for negentropy event type

def _require_len(name: str, value: bytes, expected: int) -> bytes:
    if len(value) != expected:
        raise ValueError(f"{name} must be {expected} bytes, got {len(value)}")
    return value


def encode_plaintext(
    connection_id: bytes,
    reply_connection_id: bytes,
    msg_type: int,
    range_id: bytes,
    range_start: int,
    range_end: int,
    hash_bytes: bytes,
    root_hash: bytes,
    total_events: int,
    event_count: int = 0,
    cursor: int = 0,
    has_more: bool = False,
) -> bytes:
    """Encode a negentropy payload plaintext.

    Layout (113 bytes, padded to WIRE_PLAINTEXT_SIZE):
    - connection_id (16)
    - reply_connection_id (16)
    - msg_type (1)
    - range_id (8)
    - range_start (8) - 64-bit signed integer
    - range_end (8) - 64-bit signed integer
    - hash_bytes (16)
    - root_hash (16)
    - total_events (u32)
    - event_count (u32) - for range_mismatched, or items_count for PAGE
    - cursor (8) - 64-bit signed integer for GET/PAGE
    - has_more (1) - boolean for PAGE
    """
    _require_len("connection_id", connection_id, 16)
    _require_len("reply_connection_id", reply_connection_id, 16)
    if msg_type not in (MSG_RANGE_REQUEST, MSG_RANGE_MATCHED, MSG_RANGE_MISMATCHED, MSG_RANGE_GET, MSG_RANGE_PAGE):
        raise ValueError("invalid negentropy msg_type")
    _require_len("range_id", range_id, RANGE_ID_SIZE)
    _require_len("hash_bytes", hash_bytes, 16)
    _require_len("root_hash", root_hash, 16)
    if total_events < 0 or total_events > 0xFFFFFFFF:
        raise ValueError("total_events must fit in u32")
    if event_count < 0 or event_count > 0xFFFFFFFF:
        raise ValueError("event_count must fit in u32")

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = connection_id
    payload[16:32] = reply_connection_id
    payload[32] = msg_type
    payload[33:41] = range_id
    struct.pack_into("<q", payload, 41, range_start)  # signed 64-bit
    struct.pack_into("<q", payload, 49, range_end)    # signed 64-bit
    payload[57:73] = hash_bytes
    payload[73:89] = root_hash
    struct.pack_into("<I", payload, 89, total_events)
    struct.pack_into("<I", payload, 93, event_count)
    struct.pack_into("<q", payload, 97, cursor)       # signed 64-bit cursor
    payload[105] = 1 if has_more else 0
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a negentropy payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"negentropy plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    connection_id = data[0:16]
    reply_connection_id = data[16:32]
    msg_type = data[32]
    range_id = data[33:41]
    (range_start,) = struct.unpack_from("<q", data, 41)
    (range_end,) = struct.unpack_from("<q", data, 49)
    hash_bytes = data[57:73]
    root_hash = data[73:89]
    (total_events,) = struct.unpack_from("<I", data, 89)
    (event_count,) = struct.unpack_from("<I", data, 93)
    # New fields for GET/PAGE
    cursor = 0
    has_more = False
    if len(data) >= 106:
        (cursor,) = struct.unpack_from("<q", data, 97)
        has_more = data[105] != 0
    return {
        "connection_id": connection_id,
        "reply_connection_id": reply_connection_id,
        "msg_type": msg_type,
        "range_id": range_id,
        "range_start": range_start,
        "range_end": range_end,
        "hash_bytes": hash_bytes,
        "root_hash": root_hash,
        "total_events": total_events,
        "event_count": event_count,
        "cursor": cursor,
        "has_more": has_more,
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a negentropy wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def _encode_range_id(range_id: str | None) -> bytes:
    if not range_id:
        return b"\x00" * RANGE_ID_SIZE
    raw = bytes.fromhex(range_id)
    return _require_len("range_id", raw, RANGE_ID_SIZE)


def _decode_range_id(data: bytes) -> str:
    _require_len("range_id", data, RANGE_ID_SIZE)
    return data.hex()


def _encode_hash(hex_value: str | None) -> bytes:
    if not hex_value:
        return b"\x00" * 16
    raw = bytes.fromhex(hex_value)
    return _require_len("hash", raw, 16)


def _decode_hash(data: bytes) -> str | None:
    _require_len("hash", data, 16)
    if data == b"\x00" * 16:
        return None
    return data.hex()


def encode_wire_event(
    *,
    connection_id_b64: str,
    reply_connection_id_b64: str,
    msg: dict[str, Any],
    created_at_ms: int,
) -> bytes:
    """Encode a complete negentropy wire event."""
    msg_type = msg.get("type")
    if msg_type == "range_request":
        msg_type_id = MSG_RANGE_REQUEST
    elif msg_type == "range_matched":
        msg_type_id = MSG_RANGE_MATCHED
    elif msg_type == "range_mismatched":
        msg_type_id = MSG_RANGE_MISMATCHED
    elif msg_type == "range_get":
        msg_type_id = MSG_RANGE_GET
    elif msg_type == "range_page":
        msg_type_id = MSG_RANGE_PAGE
    else:
        raise ValueError(f"unknown negentropy msg type: {msg_type}")

    range_id = _encode_range_id(msg.get("range_id"))
    range_start = int(msg.get("start", 0))
    range_end = int(msg.get("end", 0))
    hash_bytes = _encode_hash(msg.get("hash"))
    root_hash = _encode_hash(msg.get("root_hash"))
    total_events = int(msg.get("total_events") or 0)

    # For batch requests, encode batch number in event_count field (with high bit set)
    batch = msg.get("batch")
    if batch is not None:
        event_count = (1 << 31) | batch  # High bit set indicates batch request
    else:
        # event_count is also used for: items_count (PAGE), limit (GET)
        event_count = int(msg.get("event_count") or msg.get("items_count") or msg.get("limit") or 0)

    # GET/PAGE fields
    cursor = int(msg.get("cursor", 0))
    has_more = bool(msg.get("has_more", False))

    plaintext = encode_plaintext(
        connection_id=crypto.b64decode(connection_id_b64),
        reply_connection_id=crypto.b64decode(reply_connection_id_b64),
        msg_type=msg_type_id,
        range_id=range_id,
        range_start=range_start,
        range_end=range_end,
        hash_bytes=hash_bytes,
        root_hash=root_hash,
        total_events=total_events,
        event_count=event_count,
        cursor=cursor,
        has_more=has_more,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_UNSIGNED,
        signer_type=wire_format.SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * wire_format.SIGNER_ID_SIZE,
    )
    payload = wire_format._pad_payload(plaintext)
    signature = b"\x00" * wire_format.SIGNATURE_SIZE
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a negentropy wire event."""
    header, payload, _signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for negentropy")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)

    msg_type = decoded["msg_type"]
    if msg_type == MSG_RANGE_REQUEST:
        msg_type_str = "range_request"
    elif msg_type == MSG_RANGE_MATCHED:
        msg_type_str = "range_matched"
    elif msg_type == MSG_RANGE_MISMATCHED:
        msg_type_str = "range_mismatched"
    elif msg_type == MSG_RANGE_GET:
        msg_type_str = "range_get"
    elif msg_type == MSG_RANGE_PAGE:
        msg_type_str = "range_page"
    else:
        raise ValueError("invalid negentropy msg_type")

    msg_result: dict[str, Any] = {
        "type": msg_type_str,
        "range_id": _decode_range_id(decoded["range_id"]),
        "root_hash": _decode_hash(decoded["root_hash"]) or "",
        "total_events": decoded["total_events"],
    }

    if msg_type_str == "range_request":
        msg_result["start"] = decoded["range_start"]
        msg_result["end"] = decoded["range_end"]
        msg_result["hash"] = _decode_hash(decoded["hash_bytes"]) or ""
        # Check if this is a batch request (high bit set in event_count)
        event_count = decoded["event_count"]
        if event_count & (1 << 31):
            msg_result["batch"] = event_count & ((1 << 31) - 1)
    elif msg_type_str == "range_mismatched":
        msg_result["event_count"] = decoded["event_count"]
        msg_result["start"] = decoded["range_start"]
        msg_result["end"] = decoded["range_end"]
    elif msg_type_str == "range_get":
        msg_result["start"] = decoded["range_start"]
        msg_result["end"] = decoded["range_end"]
        msg_result["cursor"] = decoded["cursor"]
        msg_result["limit"] = decoded["event_count"]  # Reuse event_count as limit
    elif msg_type_str == "range_page":
        msg_result["items_count"] = decoded["event_count"]
        msg_result["cursor"] = decoded["cursor"]
        msg_result["has_more"] = decoded["has_more"]

    event_data = {
        "type": "negentropy",
        "connection_id": crypto.b64encode(decoded["connection_id"]),
        "reply_connection_id": crypto.b64encode(decoded["reply_connection_id"]),
        "data": msg_result,
        "created_at": header.created_at_ms,
    }
    return event_data


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for negentropy sync messages.

    Negentropy messages are ephemeral sync protocol messages. They don't write
    to any projection table - instead, they trigger the sync state machine via
    a command.
    """
    event_data = ctx.event_data
    connection_id = event_data.get('reply_connection_id')

    if not connection_id:
        log.warning(f"negentropy.project_pure: missing reply_connection_id in {ctx.event_id[:20]}...")
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Return a command to handle the sync message (side effect)
    commands = (
        Command(
            command_type='handle_negentropy_sync',
            args={
                'connection_id': connection_id,
                'event_data': event_data,
            }
        ),
    )

    return ProjectorResult(writes=tuple(), valid_event=True, commands=commands)


def _handle_negentropy_sync(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Handle negentropy sync command.

    Dispatches the sync message to handle_incoming().
    """
    connection_id = args['connection_id']
    event_data = args['event_data']
    handle_incoming(db, recorded_by, connection_id, event_data, recorded_at)


# Register the command handler at module load time
register_command_handler('handle_negentropy_sync', _handle_negentropy_sync)




# ============================================================================
# Congestion Control - FUTURE WORK
# ============================================================================
# Congestion control should be implemented at the transport layer, not here.
# The sync protocol is stateless and doesn't need to track in-flight requests.
#
# Future CC design goals:
# - Transport-level windowing (not per-protocol)
# - RTT measurement via transport heartbeats
# - Backpressure from transport to protocol layer
# - Separate from sync semantics
#
# For now, sync sends requests freely and relies on transport for delivery.


class RangeStatus(Enum):
    PENDING = 'pending'          # Waiting for their response
    MATCHED = 'matched'          # Hashes match, range synced
    DIVERGED = 'diverged'        # Hashes differ, need to drill down or send
    EVENTS_SENT = 'events_sent'  # We sent events, waiting for their events
    COMPLETE = 'complete'        # Range fully reconciled


@dataclass
class RangeRequest:
    """A request to sync a specific unified key prefix range."""
    range_id: str
    level: str
    prefix: str  # Hex prefix of unified key
    hashes: dict[str, bytes]  # child_prefix -> hash


@dataclass
class RangeResponse:
    """Response to a range request."""
    range_id: str
    level: str
    prefix: str
    hashes: dict[str, bytes]


@dataclass
class EventsMessage:
    """Events for a specific bucket."""
    range_id: str
    prefix: str
    event_ids: list[str]


# ============================================================================
# Unified key computation
# ============================================================================

def compute_unified_key(event_id: str, created_at: int) -> int:
    """Compute the unified time+hash key for an event.

    The unified key combines timestamp and event hash:
    - High 48 bits: timestamp in milliseconds
    - Low 16 bits: hash of event_id (for uniform distribution)

    This provides temporal clustering (events sorted by time) with
    uniform distribution within same-timestamp events.

    Args:
        event_id: The event identifier
        created_at: Event timestamp in milliseconds

    Returns:
        64-bit integer unified key
    """
    # Always hash the event_id to get uniformly distributed bits
    # This ensures different events get different hash values
    h = hashlib.blake2b(event_id.encode('utf-8'), digest_size=2).digest()
    hash_bits = int.from_bytes(h, 'big')

    # Combine: timestamp in high 47 bits, hash in low 16 bits
    # Use 47 bits for timestamp to fit in signed 64-bit SQLite INTEGER
    # This supports timestamps up to year 6429 (47 bits of milliseconds)
    timestamp_masked = created_at & 0x7FFFFFFFFFFF  # 47 bits
    return (timestamp_masked << 16) | hash_bits


# ============================================================================
# XOR Fingerprint Hashing
# ============================================================================

# Zero hash for empty ranges (16 bytes)
ZERO_HASH = b'\x00' * 16


def compute_fingerprint(event_id: str) -> bytes:
    """Compute fingerprint for an event.

    Uses event_id hash bytes directly when available to avoid re-hashing.
    Falls back to BLAKE2b for non-standard test IDs.
    """
    try:
        raw = crypto.b64decode(event_id)
        if len(raw) >= 16:
            return raw[:16]
    except Exception:
        pass

    return hashlib.blake2b(event_id.encode('utf-8'), digest_size=16).digest()


def xor_bytes(a: bytes, b: bytes) -> bytes:
    """XOR two byte strings of equal length."""
    return bytes(x ^ y for x, y in zip(a, b))


# ============================================================================
# Chunk-based fingerprint functions for O(1) range queries
# ============================================================================

def get_chunk_start(unified_key: int) -> int:
    """Get the chunk boundary for a unified key.

    Chunks are aligned to CHUNK_SIZE boundaries.
    """
    return (unified_key // CHUNK_SIZE) * CHUNK_SIZE


def update_chunk_on_insert(db, recorded_by: str, event_id: str, unified_key: int) -> None:
    """Update chunk fingerprint when inserting an event.

    XORs the event's fingerprint into the appropriate chunk.
    Creates the chunk if it doesn't exist.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    chunk_start = get_chunk_start(unified_key)
    fingerprint = compute_fingerprint(event_id)

    # Try to update existing chunk
    existing = safedb.query_one("""
        SELECT fp, cnt FROM negentropy_chunks
        WHERE recorded_by = ? AND chunk_start = ?
    """, (recorded_by, chunk_start))

    if existing:
        # XOR new fingerprint into existing
        new_fp = xor_bytes(existing['fp'], fingerprint)
        safedb.execute("""
            UPDATE negentropy_chunks
            SET fp = ?, cnt = cnt + 1
            WHERE recorded_by = ? AND chunk_start = ?
        """, (new_fp, recorded_by, chunk_start))
    else:
        # Create new chunk
        safedb.execute("""
            INSERT INTO negentropy_chunks (recorded_by, chunk_start, cnt, fp)
            VALUES (?, ?, 1, ?)
        """, (recorded_by, chunk_start, fingerprint))


def update_chunk_on_delete(db, recorded_by: str, event_id: str, unified_key: int) -> None:
    """Update chunk fingerprint when deleting an event.

    XORs the event's fingerprint out of the chunk (XOR is self-inverse).
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    chunk_start = get_chunk_start(unified_key)
    fingerprint = compute_fingerprint(event_id)

    existing = safedb.query_one("""
        SELECT fp, cnt FROM negentropy_chunks
        WHERE recorded_by = ? AND chunk_start = ?
    """, (recorded_by, chunk_start))

    if existing:
        new_fp = xor_bytes(existing['fp'], fingerprint)
        new_cnt = existing['cnt'] - 1

        if new_cnt <= 0:
            # Delete empty chunk
            safedb.execute("""
                DELETE FROM negentropy_chunks
                WHERE recorded_by = ? AND chunk_start = ?
            """, (recorded_by, chunk_start))
        else:
            safedb.execute("""
                UPDATE negentropy_chunks
                SET fp = ?, cnt = ?
                WHERE recorded_by = ? AND chunk_start = ?
            """, (new_fp, new_cnt, recorded_by, chunk_start))


def compute_range_hash_chunked(db, recorded_by: str, start: int, end: int) -> bytes:
    """Compute XOR fingerprint for range [start, end) using chunk summaries.

    For ranges that span complete chunks, uses precomputed chunk fingerprints.
    For partial chunks at edges, scans individual events.

    A complete chunk is one where [chunk_start, chunk_start + CHUNK_SIZE) is
    entirely within [start, end). Partial chunks at edges are scanned event-by-event.

    This provides O(chunks) performance instead of O(events).
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    result = ZERO_HASH

    # Find first complete chunk: the first chunk boundary >= start
    first_chunk = get_chunk_start(start)
    if first_chunk < start:
        first_chunk += CHUNK_SIZE  # First complete chunk starts after start

    # Find last complete chunk: the last chunk boundary where chunk_end <= end
    # A chunk is complete if chunk_start + CHUNK_SIZE <= end
    # So last_chunk is the largest chunk_start where chunk_start + CHUNK_SIZE <= end
    # Which means last_chunk <= end - CHUNK_SIZE
    if end <= CHUNK_SIZE:
        last_chunk = first_chunk - CHUNK_SIZE  # No complete chunks
    else:
        last_chunk = get_chunk_start(end - CHUNK_SIZE)
        # Ensure last_chunk + CHUNK_SIZE <= end
        if last_chunk + CHUNK_SIZE > end:
            last_chunk -= CHUNK_SIZE

    # Scan leading edge (before first complete chunk)
    edge_start = start
    edge_end = min(first_chunk, end)
    if edge_start < edge_end:
        rows = safedb.query("""
            SELECT event_id FROM negentropy_events
            WHERE recorded_by = ? AND unified_key >= ? AND unified_key < ?
        """, (recorded_by, edge_start, edge_end))
        for row in rows:
            fp = compute_fingerprint(row['event_id'])
            result = xor_bytes(result, fp)

    # XOR in complete chunks (only those fully contained in range)
    if first_chunk <= last_chunk:
        chunks = safedb.query("""
            SELECT fp FROM negentropy_chunks
            WHERE recorded_by = ? AND chunk_start >= ? AND chunk_start <= ?
        """, (recorded_by, first_chunk, last_chunk))
        for chunk in chunks:
            if chunk['fp']:
                result = xor_bytes(result, chunk['fp'])

    # Scan trailing edge (after last complete chunk)
    if first_chunk <= last_chunk:
        edge_start = last_chunk + CHUNK_SIZE
    else:
        edge_start = first_chunk  # No complete chunks

    edge_end = end
    if edge_start < edge_end:
        rows = safedb.query("""
            SELECT event_id FROM negentropy_events
            WHERE recorded_by = ? AND unified_key >= ? AND unified_key < ?
        """, (recorded_by, edge_start, edge_end))
        for row in rows:
            fp = compute_fingerprint(row['event_id'])
            result = xor_bytes(result, fp)

    return result


def compute_range_hash(db, recorded_by: str, start: int, end: int) -> bytes:
    """Compute XOR fingerprint hash for events in range [start, end).

    Uses chunk summaries for efficiency when the range spans multiple chunks.
    Falls back to individual event scanning for small ranges.

    Args:
        db: Database connection
        recorded_by: Peer ID
        start: Range start (inclusive)
        end: Range end (exclusive)

    Returns:
        16-byte XOR of all event fingerprints in range, or ZERO_HASH if empty
    """
    # Use chunked computation for ranges spanning multiple chunks
    range_size = end - start
    if range_size >= CHUNK_SIZE * 2:
        return compute_range_hash_chunked(db, recorded_by, start, end)

    # For small ranges, scan individual events
    safedb = create_safe_db(db, recorded_by=recorded_by)

    rows = safedb.query("""
        SELECT event_id FROM negentropy_events
        WHERE recorded_by = ? AND unified_key >= ? AND unified_key < ?
    """, (recorded_by, start, end))

    result = ZERO_HASH
    for row in rows:
        fingerprint = compute_fingerprint(row['event_id'])
        result = xor_bytes(result, fingerprint)

    return result


def get_events_in_range(db, recorded_by: str, start: int, end: int) -> list[str]:
    """Get all event IDs in range [start, end).

    Args:
        db: Database connection
        recorded_by: Peer ID
        start: Range start (inclusive)
        end: Range end (exclusive)

    Returns:
        List of event IDs sorted by unified_key
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    rows = safedb.query("""
        SELECT event_id FROM negentropy_events
        WHERE recorded_by = ? AND unified_key >= ? AND unified_key < ?
        ORDER BY unified_key
    """, (recorded_by, start, end))

    return [row['event_id'] for row in rows]


def get_events_in_range_batch(
    db, recorded_by: str, start: int, end: int, batch: int, batch_size: int
) -> list[str]:
    """Get a batch of event IDs in range [start, end) using LIMIT/OFFSET.

    Args:
        db: Database connection
        recorded_by: Peer ID
        start: Range start (inclusive)
        end: Range end (exclusive)
        batch: Batch number (0-indexed)
        batch_size: Number of events per batch

    Returns:
        List of event IDs for this batch, sorted by unified_key
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    offset = batch * batch_size

    rows = safedb.query("""
        SELECT event_id FROM negentropy_events
        WHERE recorded_by = ? AND unified_key >= ? AND unified_key < ?
        ORDER BY unified_key
        LIMIT ? OFFSET ?
    """, (recorded_by, start, end, batch_size, offset))

    return [row['event_id'] for row in rows]


def get_events_after_cursor(
    db, recorded_by: str, start: int, end: int, cursor: int, limit: int
) -> tuple[list[str], int, bool]:
    """Get events in range [start, end) after cursor using keyset pagination.

    Args:
        db: Database connection
        recorded_by: Peer ID
        start: Range start (inclusive)
        end: Range end (exclusive)
        cursor: Last seen unified_key (0 means start from beginning)
        limit: Maximum number of events to return

    Returns:
        Tuple of (event_ids, next_cursor, has_more)
        - event_ids: List of event IDs for this page
        - next_cursor: unified_key of last returned event (for next GET)
        - has_more: True if more events exist after this page
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Keyset pagination: WHERE unified_key > cursor
    # Fetch limit+1 to check if there are more
    rows = safedb.query("""
        SELECT event_id, unified_key FROM negentropy_events
        WHERE recorded_by = ? AND unified_key >= ? AND unified_key < ?
          AND unified_key > ?
        ORDER BY unified_key
        LIMIT ?
    """, (recorded_by, start, end, cursor, limit + 1))

    event_ids = [row['event_id'] for row in rows[:limit]]
    has_more = len(rows) > limit

    if event_ids:
        # Next cursor is the unified_key of the last event we're returning
        next_cursor = rows[min(len(rows) - 1, limit - 1)]['unified_key']
    else:
        next_cursor = cursor

    return event_ids, next_cursor, has_more


def get_event_count_in_range(db, recorded_by: str, start: int, end: int) -> int:
    """Get count of events in range [start, end).

    Args:
        db: Database connection
        recorded_by: Peer ID
        start: Range start (inclusive)
        end: Range end (exclusive)

    Returns:
        Number of events in range
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    row = safedb.query_one("""
        SELECT COUNT(*) as cnt FROM negentropy_events
        WHERE recorded_by = ? AND unified_key >= ? AND unified_key < ?
    """, (recorded_by, start, end))

    return row['cnt'] if row else 0


# ============================================================================
# Database operations
# ============================================================================

def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        return [items]
    return [items[i:i + size] for i in range(0, len(items), size)]


def _fetch_existing_event_ids(db, recorded_by: str, event_ids: list[str]) -> set[str]:
    if not event_ids:
        return set()

    safedb = create_safe_db(db, recorded_by=recorded_by)
    chunk_size = int(os.getenv("NEGENTROPY_EVENT_CHUNK", "400"))
    existing: set[str] = set()

    for chunk in _chunked(event_ids, chunk_size):
        placeholders = ",".join("?" for _ in chunk)
        sql = (
            "SELECT event_id FROM negentropy_events "
            f"WHERE recorded_by = ? AND event_id IN ({placeholders})"
        )
        params: list[Any] = [recorded_by, *chunk]
        rows = safedb.query(sql, tuple(params))
        existing.update(row['event_id'] for row in rows)

    return existing


def _insert_negentropy_events_returning(
    db,
    recorded_by: str,
    batch_data: list[tuple[str, str, str, int]],
) -> list[tuple[str, str]]:
    if not batch_data:
        return []

    max_vars = int(os.getenv("NEGENTROPY_INSERT_CHUNK", "200"))
    inserted_rows: list[tuple[str, str]] = []

    for chunk in _chunked(batch_data, max_vars):
        placeholders = ",".join("(?, ?, ?, ?)" for _ in chunk)
        sql = (
            "INSERT OR IGNORE INTO negentropy_events "
            "(recorded_by, event_id, unified_key, created_at) "
            f"VALUES {placeholders} "
            "RETURNING event_id, unified_key"
        )
        params: list[Any] = []
        for recorded_by_value, event_id, unified_key, created_at in chunk:
            params.extend([recorded_by_value, event_id, unified_key, created_at])
        rows = db._conn.execute(sql, tuple(params)).fetchall()
        inserted_rows.extend((row[0], row[1]) for row in rows)

    return inserted_rows


def add_events_to_sync_batch(
    db,
    recorded_by: str,
    events: list[tuple[str, int]],  # List of (event_id, created_at)
    defer_buckets: bool = False  # Ignored, kept for API compatibility
) -> None:
    """Add multiple events to the sync system efficiently.

    Args:
        db: Database connection
        recorded_by: Peer ID
        events: List of (event_id, created_at) tuples
        defer_buckets: Ignored (no bucket precomputation in bisection protocol)
    """
    if not events:
        return

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Build batch data with integer unified keys
    batch_data: list[tuple[str, str, int, int]] = []
    seen_event_ids: set[str] = set()
    unified_keys: list[tuple[str, int]] = []  # (event_id, unified_key) for chunk updates

    for event_id, created_at in events:
        if event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        unified_key = compute_unified_key(event_id, created_at)
        batch_data.append((recorded_by, event_id, unified_key, created_at))
        unified_keys.append((event_id, unified_key))

    # Batch insert into negentropy_events
    db._conn.executemany("""
        INSERT OR IGNORE INTO negentropy_events
        (recorded_by, event_id, unified_key, created_at)
        VALUES (?, ?, ?, ?)
    """, batch_data)

    # Update chunk fingerprints for each inserted event
    # Group events by chunk for efficient batch updates
    chunks: dict[int, list[str]] = {}
    for event_id, unified_key in unified_keys:
        chunk_start = get_chunk_start(unified_key)
        if chunk_start not in chunks:
            chunks[chunk_start] = []
        chunks[chunk_start].append(event_id)

    for chunk_start, event_ids in chunks.items():
        # Compute combined fingerprint for all events in this chunk
        combined_fp = ZERO_HASH
        for event_id in event_ids:
            fp = compute_fingerprint(event_id)
            combined_fp = xor_bytes(combined_fp, fp)

        # Update chunk
        existing = safedb.query_one("""
            SELECT fp, cnt FROM negentropy_chunks
            WHERE recorded_by = ? AND chunk_start = ?
        """, (recorded_by, chunk_start))

        if existing:
            new_fp = xor_bytes(existing['fp'], combined_fp)
            safedb.execute("""
                UPDATE negentropy_chunks
                SET fp = ?, cnt = cnt + ?
                WHERE recorded_by = ? AND chunk_start = ?
            """, (new_fp, len(event_ids), recorded_by, chunk_start))
        else:
            safedb.execute("""
                INSERT INTO negentropy_chunks (recorded_by, chunk_start, cnt, fp)
                VALUES (?, ?, ?, ?)
            """, (recorded_by, chunk_start, len(event_ids), combined_fp))


def remove_events_from_sync_batch(
    db,
    recorded_by: str,
    event_ids: list[str],
) -> int:
    """Remove events from the sync system (negentropy tables only).

    Deletes from negentropy_events and updates chunk fingerprints.
    Returns the number of events removed.
    """
    if not event_ids:
        return 0

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unique_ids = list(dict.fromkeys(event_ids))

    # First, get unified_keys for all events to update chunks
    chunk_size_env = int(os.getenv("NEGENTROPY_EVENT_CHUNK", "400"))
    events_to_remove: list[tuple[str, int]] = []  # (event_id, unified_key)

    for chunk in _chunked(unique_ids, chunk_size_env):
        placeholders = ",".join("?" for _ in chunk)
        rows = safedb.query(
            f"SELECT event_id, unified_key FROM negentropy_events WHERE recorded_by = ? AND event_id IN ({placeholders})",
            tuple([recorded_by, *chunk]),
        )
        for row in rows:
            events_to_remove.append((row['event_id'], row['unified_key']))

    if not events_to_remove:
        return 0

    # Group events by chunk for batch chunk updates
    chunks: dict[int, list[str]] = {}
    for event_id, unified_key in events_to_remove:
        chunk_start = get_chunk_start(unified_key)
        if chunk_start not in chunks:
            chunks[chunk_start] = []
        chunks[chunk_start].append(event_id)

    # Update chunk fingerprints
    for chunk_start, chunk_event_ids in chunks.items():
        combined_fp = ZERO_HASH
        for event_id in chunk_event_ids:
            fp = compute_fingerprint(event_id)
            combined_fp = xor_bytes(combined_fp, fp)

        existing = safedb.query_one("""
            SELECT fp, cnt FROM negentropy_chunks
            WHERE recorded_by = ? AND chunk_start = ?
        """, (recorded_by, chunk_start))

        if existing:
            new_fp = xor_bytes(existing['fp'], combined_fp)
            new_cnt = existing['cnt'] - len(chunk_event_ids)

            if new_cnt <= 0:
                safedb.execute("""
                    DELETE FROM negentropy_chunks
                    WHERE recorded_by = ? AND chunk_start = ?
                """, (recorded_by, chunk_start))
            else:
                safedb.execute("""
                    UPDATE negentropy_chunks
                    SET fp = ?, cnt = ?
                    WHERE recorded_by = ? AND chunk_start = ?
                """, (new_fp, new_cnt, recorded_by, chunk_start))

    # Delete from negentropy_events
    total_removed = 0
    for chunk in _chunked(unique_ids, chunk_size_env):
        placeholders = ",".join("?" for _ in chunk)
        safedb.execute(
            f"DELETE FROM negentropy_events WHERE recorded_by = ? AND event_id IN ({placeholders})",
            tuple([recorded_by, *chunk]),
        )
        total_removed += len(chunk)

    return len(events_to_remove)


def add_event_to_sync(
    db,
    recorded_by: str,
    event_id: str,
    created_at: int
) -> None:
    """Add an event to the sync system.

    Called when a new event is created or received.
    Marks all ancestor buckets as needing hash recomputation.

    For bulk operations, use add_events_to_sync_batch() instead.
    """
    add_events_to_sync_batch(db, recorded_by, [(event_id, created_at)])


# ============================================================================
# Shareable Event Tracking
# ============================================================================


def add_shareable_events_batch(
    events: list[tuple[str, int, int]],  # List of (event_id, created_at, recorded_at)
    can_share_peer_id: str,
    db,
    skip_negentropy: bool = False,
    defer_buckets: bool = False
) -> None:
    """Add multiple shareable events efficiently.

    Batches inserts for both shareable_events and negentropy tables.
    Much faster than calling add_shareable_event() in a loop for bulk operations.

    Args:
        events: List of (event_id, created_at, recorded_at) tuples
        can_share_peer_id: The peer who recorded/has these events
        db: Database connection
        skip_negentropy: If True, skip negentropy entirely
        defer_buckets: Ignored (kept for API compatibility)
    """
    if not events:
        return

    safedb = create_safe_db(db, recorded_by=can_share_peer_id)

    shareable_batch: list[tuple[str, str, int | None, int, int]] = []
    negentropy_events: list[tuple[str, int]] = []

    for event_id, created_at, recorded_at in events:
        event_id_bytes = crypto.b64decode(event_id)
        window_id = sync_window.SyncWindow.storage_window_from_event_id(event_id_bytes)
        shareable_batch.append((event_id, can_share_peer_id, created_at, recorded_at, window_id))
        if not skip_negentropy:
            negentropy_events.append((event_id, recorded_at))

    # Batch insert into shareable_events
    safedb.executemany(
        """INSERT OR IGNORE INTO shareable_events (event_id, can_share_peer_id, created_at, recorded_at, window_id)
           VALUES (?, ?, ?, ?, ?)""",
        shareable_batch,
    )

    # Batch add to negentropy (unless skipped)
    if not skip_negentropy:
        add_events_to_sync_batch(db, can_share_peer_id, negentropy_events, defer_buckets=defer_buckets)

    log.debug(f"add_shareable_events_batch: added {len(events)} events for peer={can_share_peer_id[:20]}... (defer={defer_buckets})")


def add_shareable_event(event_id: str, can_share_peer_id: str, created_at: int, recorded_at: int, db,
                        skip_negentropy: bool = False) -> None:
    """Add shareable event to both sync tracking tables.

    ARCHITECTURE NOTE: Dual Sync Tables
    ====================================
    We maintain two tables for sync tracking:

    1. shareable_events - Tracks window_id for hash-based windowing
       - created_at is always NULL for determinism (encrypted events can't provide it)

    2. negentropy_events - Used by negentropy time-based sync protocol
       - Tracks bucket_start_ms for time-hierarchy hashing
       - Uses recorded_at for bucketing (available for all events)

    Both tables track the same set of events. This function is the SINGLE place
    that adds to both, ensuring they stay synchronized. All paths that create
    shareable events (recorded.project, file_slice.batch_create_slices) call
    this function.

    Incoming synced events also flow through recorded.project(), so they
    automatically get added to both tables on the receiving side.

    For bulk operations, use add_shareable_events_batch() instead.

    Args:
        event_id: The event being marked as shareable
        can_share_peer_id: The peer who recorded/has this event and can share it
        created_at: When event was created (often NULL for encrypted events)
        recorded_at: When this peer recorded the event (always available)
        db: Database connection
        skip_negentropy: If True, skip negentropy bucket updates (caller will batch them)
    """
    add_shareable_events_batch([(event_id, created_at, recorded_at)], can_share_peer_id, db, skip_negentropy)


def get_total_event_count(db, recorded_by: str) -> int:
    """Get total number of events being synced."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one("""
        SELECT COUNT(*) as cnt FROM negentropy_events
        WHERE recorded_by = ?
    """, (recorded_by,))
    return row['cnt'] if row else 0


def get_root_hash(db, recorded_by: str) -> bytes:
    """Get the root hash for this peer's event set.

    This is the XOR fingerprint of all events (full range).
    """
    return compute_range_hash(db, recorded_by, MIN_UNIFIED_KEY, MAX_UNIFIED_KEY)


# ============================================================================
# Sync state management (per connection)
# ============================================================================

def generate_range_id() -> str:
    """Generate a unique range ID."""
    import secrets
    return secrets.token_hex(8)


def init_sync_for_connection(
    db,
    recorded_by: str,
    connection_id: str,
    t_ms: int
) -> list[dict]:
    """Initialize sync state for a new connection.

    Returns a full-range request to start sync.
    Only starts a new sync if there's no recent sync already in progress.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Don't start new sync if one is actively in progress (updated recently)
    # Stale entries (>5 seconds old) are ignored - sync may have been interrupted
    stale_threshold = t_ms - 5000

    pending = safedb.query_one("""
        SELECT 1 FROM negentropy_sync_state
        WHERE recorded_by = ? AND connection_id = ?
        AND status IN ('pending', 'mismatched', 'batching')
        AND updated_at > ?
        LIMIT 1
    """, (recorded_by, connection_id, stale_threshold))

    if pending:
        return []  # Let existing sync complete

    # Clear old sync state before starting fresh
    safedb.execute("""
        DELETE FROM negentropy_sync_state
        WHERE recorded_by = ? AND connection_id = ?
    """, (recorded_by, connection_id))

    # Get root hash (full range)
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    range_id = generate_range_id()

    # Record sync state with full range
    safedb.execute("""
        INSERT INTO negentropy_sync_state
        (recorded_by, connection_id, range_id, range_start, range_end,
         our_hash, their_hash, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)
    """, (recorded_by, connection_id, range_id, MIN_UNIFIED_KEY, MAX_UNIFIED_KEY, root_hash, t_ms, t_ms))

    return [{
        'type': 'range_request',
        'range_id': range_id,
        'start': MIN_UNIFIED_KEY,
        'end': MAX_UNIFIED_KEY,
        'hash': root_hash.hex() if root_hash else '',
        'root_hash': root_hash.hex() if root_hash else '',
        'total_events': total_events,
    }]


def handle_range_request(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int
) -> list[dict]:
    """Handle an incoming range request (responder side).

    Compares our hash with theirs and returns:
    - range_matched: hashes match, range is synced
    - range_matched + blobs: hashes differ but few events, sent blobs
    - range_mismatched: hashes differ, many events, requester should bisect/batch

    Optimization: When their_hash is ZERO_HASH (they have nothing), we skip
    hash computation entirely. If they request a specific batch, we send that
    batch of events directly.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    range_id = msg['range_id']
    start = msg['start']
    end = msg['end']
    their_hash = bytes.fromhex(msg['hash']) if msg['hash'] else ZERO_HASH
    batch = msg.get('batch')  # Optional batch number for bulk transfer

    # Get root hash and total events for all responses
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    # Handle batch request (bulk transfer mode)
    if batch is not None:
        # Use efficient LIMIT/OFFSET query instead of fetching all events
        batch_ids = get_events_in_range_batch(db, recorded_by, start, end, batch, BULK_BATCH_SIZE)

        if batch_ids:
            sent = _send_event_blobs(db, recorded_by, connection_id, batch_ids, t_ms)
            log.info(f"negentropy: sent batch {batch} ({sent} events) for range [{start}, {end})")

        return [{
            'type': 'range_matched',
            'range_id': range_id,
            'batch': batch,
            'root_hash': root_hash.hex() if root_hash else '',
            'total_events': total_events,
        }]

    # Fast path: if their_hash is ZERO_HASH, skip hash computation
    if their_hash == ZERO_HASH:
        event_count = get_event_count_in_range(db, recorded_by, start, end)

        if event_count == 0:
            # Both empty - matched
            log.info(f"negentropy.handle_range_request: range=[{start}, {end}) both empty")
            return [{
                'type': 'range_matched',
                'range_id': range_id,
                'root_hash': root_hash.hex() if root_hash else '',
                'total_events': total_events,
            }]

        if event_count <= EVENTS_THRESHOLD:
            # Few events - send blobs directly
            event_ids = get_events_in_range(db, recorded_by, start, end)
            if event_ids:
                sent = _send_event_blobs(db, recorded_by, connection_id, event_ids, t_ms)
                log.info(f"negentropy: sent {sent} event blobs for range [{start}, {end}) (their_hash=ZERO)")

            return [{
                'type': 'range_matched',
                'range_id': range_id,
                'root_hash': root_hash.hex() if root_hash else '',
                'total_events': total_events,
            }]

        # Many events, they have nothing - signal mismatch with count for batching
        log.info(f"negentropy.handle_range_request: range=[{start}, {end}) events={event_count} their_hash=ZERO, signaling mismatch")
        return [{
            'type': 'range_mismatched',
            'range_id': range_id,
            'start': start,
            'end': end,
            'event_count': event_count,
            'root_hash': root_hash.hex() if root_hash else '',
            'total_events': total_events,
        }]

    # Normal path: compute hash and compare
    our_hash = compute_range_hash(db, recorded_by, start, end)
    event_count = get_event_count_in_range(db, recorded_by, start, end)

    log.info(f"negentropy.handle_range_request: range=[{start}, {end}) events={event_count} "
             f"our_hash={our_hash.hex()[:16]} their_hash={their_hash.hex()[:16]} match={our_hash == their_hash}")

    # Record the range
    safedb.execute("""
        INSERT INTO negentropy_sync_state
        (recorded_by, connection_id, range_id, range_start, range_end,
         our_hash, their_hash, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT (recorded_by, connection_id, range_id) DO UPDATE SET
            their_hash = excluded.their_hash,
            updated_at = excluded.updated_at
    """, (recorded_by, connection_id, range_id, start, end, our_hash, their_hash, t_ms, t_ms))

    # Check for checkpoint: if their root hash matches ours
    their_root_hash = bytes.fromhex(msg.get('root_hash', '')) if msg.get('root_hash') else None
    if their_root_hash and their_root_hash == root_hash:
        _log_checkpoint(db, recorded_by, connection_id, root_hash, t_ms)

    if our_hash == their_hash:
        # Hashes match - this range is synced!
        safedb.execute("""
            UPDATE negentropy_sync_state
            SET status = 'matched', updated_at = ?
            WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
        """, (t_ms, recorded_by, connection_id, range_id))

        return [{
            'type': 'range_matched',
            'range_id': range_id,
            'root_hash': root_hash.hex() if root_hash else '',
            'total_events': total_events,
        }]

    # Hashes differ - check if we should send blobs or signal mismatch
    if event_count <= EVENTS_THRESHOLD:
        # Few events - send blobs directly
        safedb.execute("""
            UPDATE negentropy_sync_state
            SET status = 'complete', updated_at = ?
            WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
        """, (t_ms, recorded_by, connection_id, range_id))

        event_ids = get_events_in_range(db, recorded_by, start, end)

        # Send actual event blobs
        if event_ids:
            sent = _send_event_blobs(db, recorded_by, connection_id, event_ids, t_ms)
            log.info(f"negentropy: sent {sent} event blobs for range [{start}, {end})")

        # Return range_matched - the other side will request from us to get our events
        return [{
            'type': 'range_matched',
            'range_id': range_id,
            'root_hash': root_hash.hex() if root_hash else '',
            'total_events': total_events,
        }]

    else:
        # Many events - signal mismatch, requester will bisect
        safedb.execute("""
            UPDATE negentropy_sync_state
            SET status = 'mismatched', updated_at = ?
            WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
        """, (t_ms, recorded_by, connection_id, range_id))

        return [{
            'type': 'range_mismatched',
            'range_id': range_id,
            'event_count': event_count,
            'root_hash': root_hash.hex() if root_hash else '',
            'total_events': total_events,
        }]


def handle_range_mismatched(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int
) -> list[dict]:
    """Handle range_mismatched response (requester side).

    If we have nothing in this range (our_hash was ZERO_HASH), use cursor-based
    streaming with a single GET request. Otherwise, bisect and send child requests.

    Cursor-based streaming is more efficient than generating all batch requests
    at once because:
    1. Only one request is in flight at a time (lightweight)
    2. No memory pressure from generating thousands of batch requests
    3. Each round-trip transfers BULK_BATCH_SIZE events
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    range_id = msg['range_id']
    their_event_count = msg.get('event_count', 0)

    # Get the range we sent
    range_row = safedb.query_one("""
        SELECT range_start, range_end, our_hash FROM negentropy_sync_state
        WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
    """, (recorded_by, connection_id, range_id))

    if not range_row:
        log.warning(f"negentropy.handle_range_mismatched: unknown range_id {range_id}")
        return []

    start = range_row['range_start']
    end = range_row['range_end']
    our_hash = range_row['our_hash']

    # Count our events in this range for streaming decision
    our_event_count = get_event_count_in_range(db, recorded_by, start, end)

    # Use streaming if:
    # 1. We have nothing (our_hash == ZERO_HASH), OR
    # 2. They have many more events than us (difference > BULK_BATCH_SIZE)
    # This handles "newcomer to range" and "catch-up" cases efficiently
    # Note: Streaming sends ALL their events, so we may receive duplicates,
    # but this is more efficient than bisection when difference is large
    event_difference = their_event_count - our_event_count
    use_streaming = (
        (our_hash == ZERO_HASH and their_event_count > 0) or
        (event_difference > BULK_BATCH_SIZE)
    )

    log.info(f"negentropy.handle_range_mismatched: range=[{start}, {end}) "
             f"our_count={our_event_count} their_count={their_event_count} "
             f"our_hash={our_hash.hex()[:16] if our_hash else 'None'} "
             f"use_streaming={use_streaming}")

    if use_streaming:
        # Always use start/end from our stored range state
        # (msg start/end might be 0 from wire encoding defaults)
        req_start = start
        req_end = end

        log.info(f"negentropy: cursor-based streaming for range [{req_start}, {req_end}): "
                 f"{their_event_count} events")

        # Update parent range status to streaming
        safedb.execute("""
            UPDATE negentropy_sync_state
            SET status = 'streaming', updated_at = ?
            WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
        """, (t_ms, recorded_by, connection_id, range_id))

        # Create streaming state to track cursor position
        streaming_range_id = generate_range_id()
        safedb.execute("""
            INSERT INTO negentropy_streaming_state
            (recorded_by, connection_id, range_id, range_start, range_end,
             cursor, items_received, expected_total, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 0, ?, 'streaming', ?, ?)
        """, (recorded_by, connection_id, streaming_range_id, req_start, req_end,
              their_event_count, t_ms, t_ms))

        # Send single GET request with cursor=0 to start streaming
        return [{
            'type': 'range_get',
            'range_id': streaming_range_id,
            'start': req_start,
            'end': req_end,
            'cursor': 0,  # Start from beginning
            'limit': BULK_BATCH_SIZE,
        }]

    # Normal path: bisect the range
    mid = (start + end) // 2

    if mid == start or mid == end:
        # Can't bisect further - this shouldn't happen with proper thresholds
        log.warning(f"negentropy: can't bisect range [{start}, {end})")
        return []

    # Get root hash and total events
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    # Update parent range status
    safedb.execute("""
        UPDATE negentropy_sync_state
        SET status = 'mismatched', updated_at = ?
        WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
    """, (t_ms, recorded_by, connection_id, range_id))

    responses = []

    # Create left child range
    left_range_id = generate_range_id()
    left_hash = compute_range_hash(db, recorded_by, start, mid)
    safedb.execute("""
        INSERT INTO negentropy_sync_state
        (recorded_by, connection_id, range_id, range_start, range_end,
         our_hash, their_hash, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)
    """, (recorded_by, connection_id, left_range_id, start, mid, left_hash, t_ms, t_ms))

    responses.append({
        'type': 'range_request',
        'range_id': left_range_id,
        'start': start,
        'end': mid,
        'hash': left_hash.hex() if left_hash else '',
        'root_hash': root_hash.hex() if root_hash else '',
        'total_events': total_events,
    })

    # Create right child range
    right_range_id = generate_range_id()
    right_hash = compute_range_hash(db, recorded_by, mid, end)
    safedb.execute("""
        INSERT INTO negentropy_sync_state
        (recorded_by, connection_id, range_id, range_start, range_end,
         our_hash, their_hash, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)
    """, (recorded_by, connection_id, right_range_id, mid, end, right_hash, t_ms, t_ms))

    responses.append({
        'type': 'range_request',
        'range_id': right_range_id,
        'start': mid,
        'end': end,
        'hash': right_hash.hex() if right_hash else '',
        'root_hash': root_hash.hex() if root_hash else '',
        'total_events': total_events,
    })

    log.info(f"negentropy: bisected range [{start}, {end}) at {mid}")
    return responses


def _log_checkpoint(
    db,
    recorded_by: str,
    connection_id: str,
    root_hash: bytes,
    t_ms: int
) -> None:
    """Log a sync checkpoint when root hashes match.

    Also updates connection.last_synced_root_hash for skip-if-unchanged optimization.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get sync stats
    stats = safedb.query_one("""
        SELECT
            COUNT(CASE WHEN status = 'events_sent' THEN 1 END) as events_sent,
            COUNT(*) as ranges_checked
        FROM negentropy_sync_state
        WHERE recorded_by = ? AND connection_id = ?
    """, (recorded_by, connection_id))

    safedb.execute("""
        INSERT OR REPLACE INTO negentropy_checkpoints
        (recorded_by, connection_id, completed_at, root_hash,
         events_sent, events_received, ranges_checked)
        VALUES (?, ?, ?, ?, ?, 0, ?)
    """, (recorded_by, connection_id, t_ms, root_hash,
          stats['events_sent'] if stats else 0,
          stats['ranges_checked'] if stats else 0))

    # Update connection's last_synced_root_hash for skip-if-unchanged optimization
    safedb.execute("""
        UPDATE connections
        SET last_synced_root_hash = ?
        WHERE key_id = ? AND recorded_by = ?
    """, (root_hash, connection_id, recorded_by))


def handle_range_matched(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int
) -> list[dict]:
    """Handle confirmation that a range matches."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    range_id = msg['range_id']

    safedb.execute("""
        UPDATE negentropy_sync_state
        SET status = 'complete', updated_at = ?
        WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
    """, (t_ms, recorded_by, connection_id, range_id))

    # Check for checkpoint
    root_hash = get_root_hash(db, recorded_by)
    their_root_hash = bytes.fromhex(msg.get('root_hash', '')) if msg.get('root_hash') else None
    if their_root_hash and their_root_hash == root_hash:
        _log_checkpoint(db, recorded_by, connection_id, root_hash, t_ms)

    return []  # No response needed


def handle_range_get(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int
) -> list[dict]:
    """Handle incoming range GET request (responder side - cursor-based streaming).

    Returns a PAGE response with event blobs and cursor for next page.
    """
    range_id = msg['range_id']
    start = msg.get('start', MIN_UNIFIED_KEY)
    end = msg.get('end', MAX_UNIFIED_KEY)
    cursor = msg.get('cursor', 0)
    limit = msg.get('limit', BULK_BATCH_SIZE)

    # Get root hash and total events
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    # Get page of events using keyset pagination
    event_ids, next_cursor, has_more = get_events_after_cursor(db, recorded_by, start, end, cursor, limit)

    # Send event blobs
    if event_ids:
        sent = _send_event_blobs(db, recorded_by, connection_id, event_ids, t_ms)
        log.info(f"negentropy: GET handler sent {sent} events, cursor={cursor} -> {next_cursor}, has_more={has_more}")

    return [{
        'type': 'range_page',
        'range_id': range_id,
        'items_count': len(event_ids),
        'cursor': next_cursor,
        'has_more': has_more,
        'root_hash': root_hash.hex() if root_hash else '',
        'total_events': total_events,
    }]


def handle_range_page(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int
) -> list[dict]:
    """Handle incoming range PAGE response (requester side - cursor-based streaming).

    If has_more is true, sends another GET request with updated cursor.
    Otherwise, marks the streaming complete.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    range_id = msg['range_id']
    items_count = msg.get('items_count', 0)
    cursor = msg.get('cursor', 0)
    has_more = msg.get('has_more', False)

    # Get streaming state
    stream = safedb.query_one("""
        SELECT range_start, range_end, items_received, expected_total
        FROM negentropy_streaming_state
        WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
    """, (recorded_by, connection_id, range_id))

    if not stream:
        log.warning(f"negentropy.handle_range_page: unknown stream range_id {range_id}")
        return []

    start = stream['range_start']
    end = stream['range_end']
    new_items_received = stream['items_received'] + items_count

    # Update streaming state
    safedb.execute("""
        UPDATE negentropy_streaming_state
        SET cursor = ?, items_received = ?, updated_at = ?
        WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
    """, (cursor, new_items_received, t_ms, recorded_by, connection_id, range_id))

    log.info(f"negentropy: PAGE received {items_count} items, total={new_items_received}, has_more={has_more}")

    if has_more:
        # Send next GET request
        return [{
            'type': 'range_get',
            'range_id': range_id,
            'start': start,
            'end': end,
            'cursor': cursor,
            'limit': BULK_BATCH_SIZE,
        }]
    else:
        # Streaming complete
        safedb.execute("""
            UPDATE negentropy_streaming_state
            SET status = 'complete', updated_at = ?
            WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
        """, (t_ms, recorded_by, connection_id, range_id))

        # Check if root hashes match
        root_hash = get_root_hash(db, recorded_by)
        their_root_hash = bytes.fromhex(msg.get('root_hash', '')) if msg.get('root_hash') else None

        if their_root_hash and their_root_hash == root_hash:
            # Hashes match - mark parent range as complete and log checkpoint
            _log_checkpoint(db, recorded_by, connection_id, root_hash, t_ms)

            # Find and update parent range in negentropy_sync_state
            # (ranges with status='streaming' for this connection)
            safedb.execute("""
                UPDATE negentropy_sync_state
                SET status = 'complete', updated_at = ?
                WHERE recorded_by = ? AND connection_id = ? AND status = 'streaming'
            """, (t_ms, recorded_by, connection_id))

            return []
        else:
            # Hashes don't match - bisect the streaming range to find remaining differences
            # This is more efficient than restarting with a full-range request
            log.info(f"negentropy: streaming complete but hashes differ, bisecting range [{start}, {end})")

            # Update existing streaming ranges to complete so they don't block
            safedb.execute("""
                UPDATE negentropy_sync_state
                SET status = 'complete', updated_at = ?
                WHERE recorded_by = ? AND connection_id = ? AND status = 'streaming'
            """, (t_ms, recorded_by, connection_id))

            # Bisect the range to find the remaining differences
            mid = (start + end) // 2
            if mid == start or mid == end:
                # Can't bisect further - shouldn't happen normally
                log.warning(f"negentropy: can't bisect range [{start}, {end}) after streaming")
                return []

            total_events = get_total_event_count(db, recorded_by)
            responses = []

            # Create left child range
            left_range_id = generate_range_id()
            left_hash = compute_range_hash(db, recorded_by, start, mid)
            safedb.execute("""
                INSERT INTO negentropy_sync_state
                (recorded_by, connection_id, range_id, range_start, range_end,
                 our_hash, their_hash, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)
            """, (recorded_by, connection_id, left_range_id, start, mid, left_hash, t_ms, t_ms))

            responses.append({
                'type': 'range_request',
                'range_id': left_range_id,
                'start': start,
                'end': mid,
                'hash': left_hash.hex() if left_hash else '',
                'root_hash': root_hash.hex() if root_hash else '',
                'total_events': total_events,
            })

            # Create right child range
            right_range_id = generate_range_id()
            right_hash = compute_range_hash(db, recorded_by, mid, end)
            safedb.execute("""
                INSERT INTO negentropy_sync_state
                (recorded_by, connection_id, range_id, range_start, range_end,
                 our_hash, their_hash, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)
            """, (recorded_by, connection_id, right_range_id, mid, end, right_hash, t_ms, t_ms))

            responses.append({
                'type': 'range_request',
                'range_id': right_range_id,
                'start': mid,
                'end': end,
                'hash': right_hash.hex() if right_hash else '',
                'root_hash': root_hash.hex() if root_hash else '',
                'total_events': total_events,
            })

            log.info(f"negentropy: post-streaming bisection at {mid}")
            return responses


def _send_event_blobs(
    db,
    recorded_by: str,
    connection_id: str,
    event_ids: list[str],
    t_ms: int
) -> int:
    """Send actual event blobs over the connection.

    Args:
        db: Database connection
        recorded_by: Local peer ID
        connection_id: Connection to send on
        event_ids: Event IDs to send blobs for
        t_ms: Current timestamp

    Returns:
        Number of blobs sent
    """
    from events.network import connection_request as conn_module

    safedb = create_safe_db(db, recorded_by=recorded_by)
    sent = 0

    for event_id in event_ids:
        try:
            event_blob = safedb.get_shareable_blob(event_id)
            if conn_module.send(recorded_by, connection_id, event_blob, t_ms, db):
                sent += 1
                log.debug(f"negentropy: sent blob for {event_id[:20]}...")
        except Exception as e:
            log.warning(f"negentropy: failed to send blob for {event_id[:20]}...: {e}")

    return sent


def handle_sync_message(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int
) -> list[dict]:
    """Route incoming sync message to appropriate handler.

    Returns list of response messages to send.
    """
    msg_type = msg.get('type')

    if msg_type == 'range_request':
        return handle_range_request(db, recorded_by, connection_id, msg, t_ms)
    elif msg_type == 'range_matched':
        return handle_range_matched(db, recorded_by, connection_id, msg, t_ms)
    elif msg_type == 'range_mismatched':
        return handle_range_mismatched(db, recorded_by, connection_id, msg, t_ms)
    elif msg_type == 'range_get':
        return handle_range_get(db, recorded_by, connection_id, msg, t_ms)
    elif msg_type == 'range_page':
        return handle_range_page(db, recorded_by, connection_id, msg, t_ms)
    else:
        log.warning(f"negentropy: unknown message type: {msg_type}")
        return []  # Unknown message type


# ============================================================================
# Public API
# ============================================================================

def sync_connection(
    db,
    recorded_by: str,
    conn: 'Connection',
    t_ms: int
) -> int:
    """Start/continue negentropy sync over a connection.

    Sends root hash to initiate negotiation.

    Args:
        db: Database connection
        recorded_by: Local peer ID
        conn: Connection object (must have their_key_id set)
        t_ms: Current timestamp

    Returns:
        Number of messages sent
    """
    from events.network import connection_request as conn_module

    msgs = init_sync_for_connection(db, recorded_by, conn.key_id, t_ms)
    sent = 0
    for msg in msgs:
        # Wrap in negentropy envelope for ephemeral detection
        # Include reply_key_id so receiver knows which key_id to use
        blob = encode_wire_event(
            connection_id_b64=conn.key_id,
            reply_connection_id_b64=conn.their_key_id,
            msg=msg,
            created_at_ms=t_ms,
        )
        if conn_module.send(recorded_by, conn.key_id, blob, t_ms, db):
            sent += 1
    return sent


def handle_incoming(
    db,
    recorded_by: str,
    connection_id: str,
    envelope: dict,
    t_ms: int
) -> int:
    """Handle incoming negentropy message and send responses.

    Args:
        db: Database connection
        recorded_by: Local peer ID
        connection_id: Our connection_id (from envelope['reply_connection_id'])
        envelope: Parsed negentropy envelope (has 'type': 'negentropy' and 'data')
        t_ms: Current timestamp

    Returns:
        Number of response messages sent
    """
    from events.network import connection_request as conn_module

    # Unwrap the inner message
    msg = envelope.get('data', envelope)
    log.info(f"negentropy.handle_incoming: peer={recorded_by[:20]}... conn={connection_id[:20]}... msg_type={msg.get('type')}")

    # Extract sender's connection_id for reply_connection_id in responses
    sender_connection_id = envelope.get('connection_id')

    responses = handle_sync_message(db, recorded_by, connection_id, msg, t_ms)
    sent = 0
    for response in responses:
        # Wrap response in envelope
        # Include reply_connection_id so receiver knows which connection_id to use
        blob = encode_wire_event(
            connection_id_b64=connection_id,
            reply_connection_id_b64=sender_connection_id,
            msg=response,
            created_at_ms=t_ms,
        )
        if conn_module.send(recorded_by, connection_id, blob, t_ms, db):
            sent += 1
    return sent


def sync_all_connections(t_ms: int, db: Any) -> dict:
    """Sync all active connections for all local peers.

    Called by NegentropySyncJob. Iterates all local peers and their
    established connections, sending negentropy root hashes to initiate
    or continue sync.

    Sync is initiated for ALL established connections, even if our root hash
    hasn't changed - the remote peer may have new events we need to pull.

    Returns:
        Stats dict with counts
    """
    from core.db import create_unsafe_db
    from events.network import connection_request as conn_module

    unsafedb = create_unsafe_db(db)
    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    total_connections = 0
    total_messages = 0

    for peer_row in local_peers:
        peer_id = peer_row['peer_id']

        # With XOR fingerprinting, root hash is always current - just read it
        our_root_hash = get_root_hash(db, peer_id)

        # Get all active connections using connection module interface
        connections = conn_module.get_connections(peer_id, t_ms, db)

        for conn in connections:
            # Only sync on established bidirectional connections
            if not conn.can_send():
                continue

            # Skip connections without any identity
            # But allow bootstrap connections (have invite_id even if peer_shared_id is NULL)
            if not conn.peer_shared_id and not conn.invite_id:
                continue

            total_connections += 1

            sent = sync_connection(db, peer_id, conn, t_ms)
            total_messages += sent

    log.info(f"negentropy.sync_all_connections: {total_connections} connections, {total_messages} sent")
    return {'connections': total_connections, 'messages_sent': total_messages}


def get_sync_status(
    db,
    recorded_by: str,
    connection_id: str
) -> dict:
    """Get sync status for UI display.

    Returns counts of ranges in each state.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    rows = safedb.query("""
        SELECT status, COUNT(*) as cnt FROM negentropy_sync_state
        WHERE recorded_by = ? AND connection_id = ?
        GROUP BY status
    """, (recorded_by, connection_id))

    status = {s.value: 0 for s in RangeStatus}
    for row in rows:
        status[row['status']] = row['cnt']

    total = sum(status.values())
    complete = status['complete'] + status['matched']

    return {
        'total_ranges': total,
        'completed_ranges': complete,
        'pending_ranges': status['pending'],
        'progress_pct': (complete / total * 100) if total > 0 else 100,
        'is_synced': total > 0 and complete == total,
        'by_status': status,
    }


def get_all_connection_sync_status(db, recorded_by: str, t_ms: int) -> dict:
    """Get sync status for all active connections of a peer.

    Only includes bidirectional connections (both parties have exchanged keys).
    Expired connections are excluded.

    Args:
        db: Database connection
        recorded_by: Local peer ID
        t_ms: Current timestamp (for expiry check)

    Returns:
        {
            'all_synced': bool,           # True if all connections are synced
            'total_connections': int,
            'synced_connections': int,
            'connections': [
                {
                    'connection_id': str,
                    'peer_shared_id': str,
                    'is_synced': bool,
                    'progress_pct': float,
                    'total_ranges': int,
                    'completed_ranges': int
                }
            ]
        }
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get active bidirectional connections (their_key is not NULL = bidirectional)
    rows = safedb.query("""
        SELECT key_id, peer_shared_id
        FROM connections
        WHERE recorded_by = ?
          AND last_handshake_ms + ttl_ms > ?
          AND their_key IS NOT NULL
        ORDER BY last_handshake_ms DESC
    """, (recorded_by, t_ms))

    connections = []
    synced_count = 0

    for row in rows:
        conn_id = row['key_id']
        status = get_sync_status(db, recorded_by, conn_id)

        conn_info = {
            'connection_id': conn_id,
            'peer_shared_id': row['peer_shared_id'],
            'is_synced': status['is_synced'],
            'progress_pct': status['progress_pct'],
            'total_ranges': status['total_ranges'],
            'completed_ranges': status['completed_ranges'],
        }
        connections.append(conn_info)

        if status['is_synced']:
            synced_count += 1

    total = len(connections)
    all_synced = (total == 0) or (synced_count == total)

    return {
        'all_synced': all_synced,
        'total_connections': total,
        'synced_connections': synced_count,
        'connections': connections,
    }


def get_global_sync_status(db, t_ms: int) -> dict:
    """Get sync status across all local peers and their active connections.

    Uses negentropy's per-connection sync state for accurate progress detection.

    Args:
        db: Database connection
        t_ms: Current timestamp (for connection expiry check)

    Returns:
        {
            'all_synced': bool,           # All connections across all peers synced
            'queue_empty': bool,          # incoming_blobs is empty
            'total_connections': int,     # Count of active connections
            'synced_connections': int,    # Count that are fully synced
            'by_peer': [                  # Per local peer breakdown
                {
                    'peer_id': str,
                    'all_synced': bool,
                    'total_connections': int,
                    'synced_connections': int,
                    'connections': [...]
                }
            ]
        }
    """
    from core.db import create_unsafe_db
    unsafedb = create_unsafe_db(db)

    # Get queue size
    queue = unsafedb.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")
    queue_empty = (queue['cnt'] if queue else 0) == 0

    # Get all local peers
    local_peers = [row['peer_id'] for row in unsafedb.query("SELECT peer_id FROM local_peers")]

    by_peer = []
    total_connections = 0
    total_synced = 0

    for peer_id in local_peers:
        status = get_all_connection_sync_status(db, peer_id, t_ms)
        by_peer.append({
            'peer_id': peer_id,
            'all_synced': status['all_synced'],
            'total_connections': status['total_connections'],
            'synced_connections': status['synced_connections'],
            'connections': status['connections'],
        })
        total_connections += status['total_connections']
        total_synced += status['synced_connections']

    # All synced if every peer is synced (vacuously true if no connections)
    all_synced = all(p['all_synced'] for p in by_peer) if by_peer else True

    return {
        'all_synced': all_synced,
        'queue_empty': queue_empty,
        'total_connections': total_connections,
        'synced_connections': total_synced,
        'by_peer': by_peer,
    }


# ============================================================================
# Debug/Utility functions
# ============================================================================

def decode_unified_key(unified_key: str) -> tuple[int, str]:
    """Decode a unified key (hash-only variant).

    In the hash-only variant, the unified key is purely derived from event_id.
    This function returns (0, full_hash) for compatibility with code that
    expects a (timestamp, hash) tuple.

    Args:
        unified_key: 16 character hex string

    Returns:
        Tuple of (0, full_hash_hex) - timestamp is always 0 in hash-only mode
    """
    return 0, unified_key


def format_unified_key_human(unified_key: str) -> str:
    """Format a unified key for human-readable display.

    In hash-only mode, just shows the hash prefix since there's no timestamp.
    """
    return f"hash:{unified_key[:8]}..."
