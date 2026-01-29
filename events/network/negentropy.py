"""
Negentropy-style deterministic sync protocol with adaptive depth splitting.

This module implements set reconciliation using hierarchical bucket hashes
with time-based prefixes and adaptive splitting for large buckets.

Design goals:
- Deterministic: no false positives, exact set reconciliation
- Finality: clear "done" state when synced (root hashes match)
- Fossilization: old time buckets stop changing and can be cached
- Scalable: handles large files (1GB+) without range explosion

Unified key format (16 hex chars = 8 bytes):
- time_hex (6 chars) = coarse timestamp (~4.4 minute buckets)
- hash (10 chars) = event_id hash prefix

Bucket hierarchy:
- root → prefix_2 → prefix_4 → prefix_6 (time-based, deterministic)
- At prefix_6 with many events: switch to adaptive binary splitting

Adaptive splitting (within time buckets):
- When a prefix_6 bucket has > EVENTS_THRESHOLD events, split by median
- Binary splits use lo_key/hi_key bounds for precise ranges
- Continues splitting until each range has ≤ EVENTS_THRESHOLD events
- Avoids "range explosion" for large files with same timestamp

Protocol flow:
1. On new connection: send root hash
2. If roots match: synced (log checkpoint)
3. If roots differ: drill down through prefix levels
4. At prefix_6 with many events: switch to adaptive splitting
5. When range has ≤ EVENTS_THRESHOLD events: send blobs directly
6. When root hashes match: sync complete for this connection
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
MSG_RANGE_EVENTS = 3

# Negentropy level constants
LEVEL_ROOT = 0
LEVEL_PREFIX_2 = 1
LEVEL_PREFIX_4 = 2
LEVEL_PREFIX_6 = 3
LEVEL_ADAPTIVE = 0xFF  # Adaptive splitting (uses lo_key/hi_key bounds)

# Negentropy wire format sizes
RANGE_ID_SIZE = 8
PREFIX_BYTES = 3  # 3 bytes = 6 hex chars for prefix_6
UNIFIED_KEY_SIZE = 8  # 8 bytes = 16 hex chars (time + hash)
EVENT_ID_MAX = 15

# Bounds flags for adaptive splitting
BOUNDS_NONE = 0
BOUNDS_HAS_LO = 1
BOUNDS_HAS_HI = 2
BOUNDS_HAS_BOTH = 3

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
    level: int,
    prefix_bytes: bytes,
    hash_bytes: bytes,
    root_hash: bytes,
    total_events: int,
    parent_range_id: bytes,
    event_ids: list[bytes],
    lo_key: bytes | None = None,
    hi_key: bytes | None = None,
) -> bytes:
    """Encode a negentropy payload plaintext.

    Layout (344 bytes):
    - connection_id (16)
    - reply_connection_id (16)
    - msg_type (1)
    - range_id (8)
    - level (1)
    - prefix_len (1)
    - prefix_bytes (3)
    - hash_bytes (16)
    - root_hash (16)
    - total_events (u32)
    - parent_range_id (8)
    - bounds_flags (1)
    - lo_key (8) - adaptive range lower bound
    - hi_key (8) - adaptive range upper bound
    - event_count (1)
    - event_ids (15 * 16 = 240) - legacy, rarely used
    """
    _require_len("connection_id", connection_id, 16)
    _require_len("reply_connection_id", reply_connection_id, 16)
    if msg_type not in (MSG_RANGE_REQUEST, MSG_RANGE_MATCHED, MSG_RANGE_EVENTS):
        raise ValueError("invalid negentropy msg_type")
    _require_len("range_id", range_id, RANGE_ID_SIZE)
    if level not in (LEVEL_ROOT, LEVEL_PREFIX_2, LEVEL_PREFIX_4, LEVEL_PREFIX_6, LEVEL_ADAPTIVE):
        raise ValueError("invalid negentropy level")
    if len(prefix_bytes) > PREFIX_BYTES:
        raise ValueError("prefix_bytes exceeds max")
    _require_len("hash_bytes", hash_bytes, 16)
    _require_len("root_hash", root_hash, 16)
    if total_events < 0 or total_events > 0xFFFFFFFF:
        raise ValueError("total_events must fit in u32")
    _require_len("parent_range_id", parent_range_id, RANGE_ID_SIZE)
    if len(event_ids) > EVENT_ID_MAX:
        raise ValueError("event_ids exceeds max")
    for event_id in event_ids:
        _require_len("event_id", event_id, 16)

    # Compute bounds flags
    bounds_flags = BOUNDS_NONE
    if lo_key:
        _require_len("lo_key", lo_key, UNIFIED_KEY_SIZE)
        bounds_flags |= BOUNDS_HAS_LO
    if hi_key:
        _require_len("hi_key", hi_key, UNIFIED_KEY_SIZE)
        bounds_flags |= BOUNDS_HAS_HI

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = connection_id
    payload[16:32] = reply_connection_id
    payload[32] = msg_type
    payload[33:41] = range_id
    payload[41] = level
    payload[42] = len(prefix_bytes)
    payload[43:43 + len(prefix_bytes)] = prefix_bytes
    payload[46:62] = hash_bytes
    payload[62:78] = root_hash
    struct.pack_into("<I", payload, 78, total_events)
    payload[82:90] = parent_range_id
    # Adaptive splitting bounds
    payload[90] = bounds_flags
    if lo_key:
        payload[91:99] = lo_key
    if hi_key:
        payload[99:107] = hi_key
    # Event IDs (legacy)
    payload[107] = len(event_ids)
    cursor = 108
    for event_id in event_ids:
        payload[cursor:cursor + 16] = event_id
        cursor += 16
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
    level = data[41]
    prefix_len = data[42]
    if prefix_len > PREFIX_BYTES:
        raise ValueError("invalid prefix length")
    prefix_bytes_data = data[43:43 + prefix_len]
    hash_bytes = data[46:62]
    root_hash = data[62:78]
    (total_events,) = struct.unpack_from("<I", data, 78)
    parent_range_id = data[82:90]
    # Adaptive splitting bounds
    bounds_flags = data[90]
    lo_key = data[91:99] if (bounds_flags & BOUNDS_HAS_LO) else None
    hi_key = data[99:107] if (bounds_flags & BOUNDS_HAS_HI) else None
    # Event IDs (legacy)
    event_count = data[107]
    if event_count > EVENT_ID_MAX:
        raise ValueError("negentropy event_count exceeds max")
    event_ids_list: list[bytes] = []
    cursor = 108
    for _ in range(event_count):
        event_ids_list.append(data[cursor:cursor + 16])
        cursor += 16
    return {
        "connection_id": connection_id,
        "reply_connection_id": reply_connection_id,
        "msg_type": msg_type,
        "range_id": range_id,
        "level": level,
        "prefix_bytes": prefix_bytes_data,
        "hash_bytes": hash_bytes,
        "root_hash": root_hash,
        "total_events": total_events,
        "parent_range_id": parent_range_id,
        "lo_key": lo_key,
        "hi_key": hi_key,
        "event_ids": event_ids_list,
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


def _encode_prefix(prefix: str | None) -> bytes:
    if not prefix:
        return b""
    if len(prefix) % 2 != 0:
        raise ValueError("prefix must be even-length hex")
    raw = bytes.fromhex(prefix)
    if len(raw) > PREFIX_BYTES:
        raise ValueError("prefix too long")
    return raw


def _decode_prefix(prefix_bytes_data: bytes) -> str:
    return prefix_bytes_data.hex()


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


def _encode_unified_key(key_hex: str | None) -> bytes | None:
    """Encode a unified key (16 hex chars -> 8 bytes)."""
    if not key_hex:
        return None
    raw = bytes.fromhex(key_hex)
    return _require_len("unified_key", raw, UNIFIED_KEY_SIZE)


def _decode_unified_key(data: bytes | None) -> str | None:
    """Decode a unified key (8 bytes -> 16 hex chars)."""
    if not data:
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
    elif msg_type == "range_events":
        msg_type_id = MSG_RANGE_EVENTS
    else:
        raise ValueError("unknown negentropy msg type")

    level_name = msg.get("level")
    if level_name == "root":
        level_id = LEVEL_ROOT
    elif level_name == "prefix_2":
        level_id = LEVEL_PREFIX_2
    elif level_name == "prefix_4":
        level_id = LEVEL_PREFIX_4
    elif level_name == "prefix_6":
        level_id = LEVEL_PREFIX_6
    elif level_name == "adaptive":
        level_id = LEVEL_ADAPTIVE
    else:
        level_id = LEVEL_ROOT

    range_id = _encode_range_id(msg.get("range_id"))
    prefix_bytes_encoded = _encode_prefix(msg.get("prefix"))

    if msg_type_id == MSG_RANGE_EVENTS:
        hash_bytes = _encode_hash(msg.get("our_hash"))
    else:
        hash_bytes = _encode_hash(msg.get("hash"))

    root_hash = _encode_hash(msg.get("root_hash"))
    total_events = int(msg.get("total_events") or 0)
    parent_range_id = _encode_range_id(msg.get("parent_range_id"))

    # Adaptive splitting bounds
    lo_key = _encode_unified_key(msg.get("lo_key"))
    hi_key = _encode_unified_key(msg.get("hi_key"))

    event_ids: list[bytes] = []
    if msg_type_id == MSG_RANGE_EVENTS:
        for event_id_b64 in msg.get("event_ids", []):
            event_ids.append(_require_len("event_id", crypto.b64decode(event_id_b64), 16))

    plaintext = encode_plaintext(
        connection_id=crypto.b64decode(connection_id_b64),
        reply_connection_id=crypto.b64decode(reply_connection_id_b64),
        msg_type=msg_type_id,
        range_id=range_id,
        level=level_id,
        prefix_bytes=prefix_bytes_encoded,
        hash_bytes=hash_bytes,
        root_hash=root_hash,
        total_events=total_events,
        parent_range_id=parent_range_id,
        event_ids=event_ids,
        lo_key=lo_key,
        hi_key=hi_key,
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
    elif msg_type == MSG_RANGE_EVENTS:
        msg_type_str = "range_events"
    else:
        raise ValueError("invalid negentropy msg_type")

    level_value = decoded["level"]
    if level_value == LEVEL_ROOT:
        level_str = "root"
    elif level_value == LEVEL_PREFIX_2:
        level_str = "prefix_2"
    elif level_value == LEVEL_PREFIX_4:
        level_str = "prefix_4"
    elif level_value == LEVEL_PREFIX_6:
        level_str = "prefix_6"
    elif level_value == LEVEL_ADAPTIVE:
        level_str = "adaptive"
    else:
        raise ValueError("invalid negentropy level")

    msg_result: dict[str, Any] = {
        "type": msg_type_str,
        "range_id": _decode_range_id(decoded["range_id"]),
        "root_hash": _decode_hash(decoded["root_hash"]) or "",
        "total_events": decoded["total_events"],
    }
    prefix_str = _decode_prefix(decoded["prefix_bytes"])
    if prefix_str:
        msg_result["prefix"] = prefix_str

    # Adaptive splitting bounds
    lo_key = _decode_unified_key(decoded.get("lo_key"))
    hi_key = _decode_unified_key(decoded.get("hi_key"))
    if lo_key:
        msg_result["lo_key"] = lo_key
    if hi_key:
        msg_result["hi_key"] = hi_key

    if msg_type_str == "range_request":
        msg_result["level"] = level_str
        msg_result["prefix"] = prefix_str
        msg_result["hash"] = _decode_hash(decoded["hash_bytes"]) or ""
        parent_range = _decode_range_id(decoded["parent_range_id"])
        if parent_range != "0000000000000000":
            msg_result["parent_range_id"] = parent_range
    elif msg_type_str == "range_events":
        msg_result["our_hash"] = _decode_hash(decoded["hash_bytes"]) or ""
        msg_result["event_ids"] = [crypto.b64encode(event_id) for event_id in decoded["event_ids"]]
        msg_result["prefix"] = prefix_str
    else:
        pass

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


# Hierarchy levels - time prefix (6 chars) + hash suffix (up to 4 chars)
# root (0) -> prefix_2 -> prefix_4 -> prefix_6 -> prefix_8 -> prefix_10
#
# For file slices with same created_at (same time prefix):
# - prefix_6: all in 1 bucket (time only)
# - prefix_8: 256 sub-buckets by hash
# - prefix_10: 65,536 sub-buckets by hash
#
# For 1GB file (2.4M slices):
# - prefix_6: 1 bucket × 2.4M events (too many!)
# - prefix_8: 256 buckets × ~9,400 events each
# - prefix_10: 65,536 buckets × ~37 events each (under threshold)
# Hierarchy levels - deterministic time-based buckets up to prefix_6
# After prefix_6, switch to adaptive splitting within time buckets
LEVELS = ['root', 'prefix_2', 'prefix_4', 'prefix_6']

# Hex characters per level (time-based up to prefix_6)
# unified_key = time_hex (6 chars) + hash (10 chars)
LEVEL_PREFIX_LEN = {
    'root': 0,
    'prefix_2': 2,     # 256 buckets (~19 hours each)
    'prefix_4': 4,     # 65,536 buckets (~4.4 minutes each)
    'prefix_6': 6,     # 16.7M buckets (~1 second each) - time boundary
    'adaptive': 16,    # Full unified key for adaptive ranges
}

# When bucket has this many events or fewer, send event blobs directly
# Larger values reduce round trips but increase message size
EVENTS_THRESHOLD = 100


# ============================================================================
# Congestion Control State
# ============================================================================
# Per-connection adaptive windowing based on RTT measurement.
# See docs/quiet-protocol-specification.md "Congestion control" section.

@dataclass
class CCState:
    """Congestion control state for a single connection."""
    window: int = 1              # Max in-flight range operations
    in_flight: int = 0           # Currently awaiting response
    rtt_ms: float = 200.0        # RTT estimate (exponential moving average)
    last_send_ms: int = 0        # Time of oldest in-flight request


# Constants for congestion control
CC_MIN_WINDOW = 1
CC_MAX_WINDOW = 32
CC_RTT_ALPHA = 0.2               # EMA smoothing factor
CC_TIMEOUT_MULTIPLIER = 3        # Timeout = 3 * RTT


# Module-level CC state per connection
# Key: (recorded_by, connection_id) tuple
_cc_state: dict[tuple[str, str], CCState] = {}


def _get_cc_state(recorded_by: str, connection_id: str) -> CCState:
    """Get or create CC state for a connection."""
    key = (recorded_by, connection_id)
    if key not in _cc_state:
        _cc_state[key] = CCState()
    return _cc_state[key]


def _cc_can_send(recorded_by: str, connection_id: str) -> bool:
    """Check if congestion control allows sending more requests."""
    state = _get_cc_state(recorded_by, connection_id)
    return state.in_flight < state.window


def _cc_on_send(recorded_by: str, connection_id: str, t_ms: int) -> None:
    """Record that we sent a range request."""
    state = _get_cc_state(recorded_by, connection_id)
    if state.in_flight == 0:
        state.last_send_ms = t_ms
    state.in_flight += 1


def _cc_on_response(recorded_by: str, connection_id: str, t_ms: int) -> None:
    """Record that we received a response (range_matched or range_response)."""
    state = _get_cc_state(recorded_by, connection_id)
    if state.in_flight > 0:
        state.in_flight -= 1

    if state.in_flight == 0 and state.last_send_ms > 0:
        # All requests answered - measure RTT and grow window
        rtt_sample = t_ms - state.last_send_ms
        if rtt_sample > 0:
            state.rtt_ms = (1 - CC_RTT_ALPHA) * state.rtt_ms + CC_RTT_ALPHA * rtt_sample
        state.window = min(state.window + 1, CC_MAX_WINDOW)
        log.debug(f"CC: conn={connection_id[:16]}... RTT={state.rtt_ms:.0f}ms window={state.window}")


def _cc_check_timeout(recorded_by: str, connection_id: str, t_ms: int) -> bool:
    """Check for timeout and shrink window if needed. Returns True if timed out."""
    state = _get_cc_state(recorded_by, connection_id)
    if state.in_flight > 0 and state.last_send_ms > 0:
        timeout_ms = CC_TIMEOUT_MULTIPLIER * state.rtt_ms
        if (t_ms - state.last_send_ms) > timeout_ms:
            # Timeout - shrink window and reset in_flight
            old_window = state.window
            state.window = max(CC_MIN_WINDOW, state.window // 2)
            state.in_flight = 0
            log.info(f"CC: timeout conn={connection_id[:16]}... window {old_window}->{state.window}")
            return True
    return False


def _cc_reset(recorded_by: str, connection_id: str) -> None:
    """Reset CC state for a connection (e.g., on disconnect)."""
    key = (recorded_by, connection_id)
    if key in _cc_state:
        del _cc_state[key]


def _cc_reset_all() -> None:
    """Reset all CC state. For testing only."""
    _cc_state.clear()


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

def compute_unified_key(event_id: str, created_at: int = 0) -> str:
    """Compute the unified key for an event: time_hex (6 chars) + hash (10 chars).

    The unified key combines timestamp and event_id hash:
    - First 6 hex chars (3 bytes, 24 bits) = timestamp / 256 (4.4-minute buckets)
    - Next 10 hex chars (5 bytes, 40 bits) = event_id hash prefix

    This provides:
    - Temporal locality: events cluster by time in the prefix tree
    - Fossilization: old time buckets stop changing and can be cached
    - Uniform distribution within time buckets: large files spread evenly

    Args:
        event_id: The event identifier (base64-encoded hash)
        created_at: Timestamp in milliseconds

    Returns:
        16 character hex string (64 bits total)
    """
    # Time component: 24 bits of coarse timestamp (~4.4 minute buckets)
    # Divide by 256*1000 to get ~4.4 minute granularity
    coarse_time = (created_at // 256000) & 0xFFFFFF  # 24 bits
    time_hex = f"{coarse_time:06x}"  # 6 hex chars

    # Hash component: 40 bits from event_id
    # Only use raw bytes if event_id looks like a proper base64-encoded hash
    # (real event_ids are 16 or 32 byte hashes, base64-encoded to ~22 or ~43 chars)
    try:
        raw = crypto.b64decode(event_id)
        # Real event_ids decode to 16 or 32 bytes (hash outputs)
        if len(raw) in (16, 32):
            hash_hex = raw[:5].hex()  # 10 hex chars
        else:
            # Not a real event_id, use blake2b
            h = hashlib.blake2b(event_id.encode('utf-8'), digest_size=5).digest()
            hash_hex = h.hex()
    except Exception:
        h = hashlib.blake2b(event_id.encode('utf-8'), digest_size=5).digest()
        hash_hex = h.hex()

    return time_hex + hash_hex  # 16 hex chars total


def get_prefix_for_level(event_id: str, created_at: int, level: str) -> str:
    """Get the bucket prefix for an event at a given level."""
    unified_key = compute_unified_key(event_id, created_at)
    prefix_len = LEVEL_PREFIX_LEN.get(level, 0)
    return unified_key[:prefix_len]


def get_child_level(level: str) -> Optional[str]:
    """Get the next finer level."""
    idx = LEVELS.index(level)
    if idx >= len(LEVELS) - 1:
        return None
    return LEVELS[idx + 1]


def get_parent_level(level: str) -> Optional[str]:
    """Get the next coarser level."""
    idx = LEVELS.index(level)
    if idx <= 0:
        return None
    return LEVELS[idx - 1]


def get_child_prefix_len(level: str) -> int:
    """Get the prefix length for children of this level."""
    child_level = get_child_level(level)
    if child_level is None:
        return LEVEL_PREFIX_LEN[level]
    return LEVEL_PREFIX_LEN[child_level]


# ============================================================================
# XOR Fingerprint Hashing (O(1) bucket updates)
# ============================================================================

# Zero hash for empty buckets (16 bytes)
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


def xor_into_bucket(db, recorded_by: str, level: str, prefix: str, fingerprint: bytes, t_ms: int) -> None:
    """XOR a fingerprint into a bucket hash.

    Creates bucket with the fingerprint if it doesn't exist.
    Updates existing bucket by XORing the fingerprint into its hash.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get current hash (or zero if bucket doesn't exist)
    row = safedb.query_one("""
        SELECT hash FROM negentropy_buckets
        WHERE recorded_by = ? AND level = ? AND prefix = ?
    """, (recorded_by, level, prefix))

    if row and row['hash']:
        current_hash = row['hash']
        new_hash = xor_bytes(current_hash, fingerprint)
    else:
        # New bucket starts with just this fingerprint
        new_hash = fingerprint

    # Upsert the bucket with new hash
    safedb.execute("""
        INSERT INTO negentropy_buckets
        (recorded_by, level, prefix, hash, event_count, updated_at)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT (recorded_by, level, prefix) DO UPDATE SET
            hash = excluded.hash,
            event_count = event_count + 1,
            updated_at = excluded.updated_at
    """, (recorded_by, level, prefix, new_hash, t_ms))


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


def _apply_bucket_deltas(
    db,
    recorded_by: str,
    new_events: list[tuple[str, str]],
) -> None:
    if not new_events:
        return

    safedb = create_safe_db(db, recorded_by=recorded_by)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)

    bucket_xors: dict[tuple[str, str], bytes] = {}
    bucket_counts: dict[tuple[str, str], int] = {}

    for event_id, unified_key in new_events:
        fingerprint = compute_fingerprint(event_id)

        for level in LEVELS:
            prefix_len = LEVEL_PREFIX_LEN[level]
            prefix = unified_key[:prefix_len]
            bucket_key = (level, prefix)
            if bucket_key in bucket_xors:
                bucket_xors[bucket_key] = xor_bytes(bucket_xors[bucket_key], fingerprint)
                bucket_counts[bucket_key] += 1
            else:
                bucket_xors[bucket_key] = fingerprint
                bucket_counts[bucket_key] = 1

    if not bucket_xors:
        return

    chunk_size = int(os.getenv("NEGENTROPY_BUCKET_CHUNK", "400"))
    existing: dict[tuple[str, str], tuple[bytes, int]] = {}
    bucket_keys = list(bucket_xors.keys())

    for chunk in _chunked(bucket_keys, chunk_size):
        placeholders = ",".join("(?, ?)" for _ in chunk)
        sql = (
            "SELECT level, prefix, hash, event_count FROM negentropy_buckets "
            f"WHERE recorded_by = ? AND (level, prefix) IN ({placeholders})"
        )
        params: list[Any] = [recorded_by]
        for level, prefix in chunk:
            params.extend([level, prefix])
        rows = safedb.query(sql, tuple(params))
        for row in rows:
            existing[(row['level'], row['prefix'])] = (row['hash'], row['event_count'] or 0)

    upsert_batch: list[tuple[str, str, str, bytes, int, int]] = []
    for bucket_key, delta_hash in bucket_xors.items():
        existing_hash, existing_count = existing.get(bucket_key, (None, 0))
        if existing_hash:
            new_hash = xor_bytes(existing_hash, delta_hash)
        else:
            new_hash = delta_hash
        new_count = existing_count + bucket_counts[bucket_key]
        level, prefix = bucket_key
        upsert_batch.append((recorded_by, level, prefix, new_hash, new_count, now))

    safedb.executemany(
        """
        INSERT INTO negentropy_buckets
        (recorded_by, level, prefix, hash, event_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (recorded_by, level, prefix) DO UPDATE SET
            hash = excluded.hash,
            event_count = excluded.event_count,
            updated_at = excluded.updated_at
        """,
        upsert_batch,
    )


def _apply_bucket_removals(
    db,
    recorded_by: str,
    removed_events: list[tuple[str, str]],
) -> None:
    if not removed_events:
        return

    safedb = create_safe_db(db, recorded_by=recorded_by)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)

    bucket_xors: dict[tuple[str, str], bytes] = {}
    bucket_counts: dict[tuple[str, str], int] = {}

    for event_id, unified_key in removed_events:
        fingerprint = compute_fingerprint(event_id)
        for level in LEVELS:
            prefix_len = LEVEL_PREFIX_LEN[level]
            prefix = unified_key[:prefix_len]
            bucket_key = (level, prefix)
            if bucket_key in bucket_xors:
                bucket_xors[bucket_key] = xor_bytes(bucket_xors[bucket_key], fingerprint)
                bucket_counts[bucket_key] += 1
            else:
                bucket_xors[bucket_key] = fingerprint
                bucket_counts[bucket_key] = 1

    if not bucket_xors:
        return

    chunk_size = int(os.getenv("NEGENTROPY_BUCKET_CHUNK", "400"))
    existing: dict[tuple[str, str], tuple[bytes, int]] = {}
    bucket_keys = list(bucket_xors.keys())

    for chunk in _chunked(bucket_keys, chunk_size):
        placeholders = ",".join("(?, ?)" for _ in chunk)
        sql = (
            "SELECT level, prefix, hash, event_count FROM negentropy_buckets "
            f"WHERE recorded_by = ? AND (level, prefix) IN ({placeholders})"
        )
        params: list[Any] = [recorded_by]
        for level, prefix in chunk:
            params.extend([level, prefix])
        rows = safedb.query(sql, tuple(params))
        for row in rows:
            existing[(row['level'], row['prefix'])] = (row['hash'], row['event_count'] or 0)

    update_batch: list[tuple[bytes, int, int, str, str, str]] = []
    delete_batch: list[tuple[str, str, str]] = []

    for bucket_key, delta_hash in bucket_xors.items():
        existing_hash, existing_count = existing.get(bucket_key, (None, 0))
        if not existing_hash or existing_count <= 0:
            continue
        new_hash = xor_bytes(existing_hash, delta_hash)
        new_count = existing_count - bucket_counts[bucket_key]
        level, prefix = bucket_key
        if new_count <= 0:
            delete_batch.append((recorded_by, level, prefix))
        else:
            update_batch.append((new_hash, new_count, now, recorded_by, level, prefix))

    if update_batch:
        safedb.executemany(
            """
            UPDATE negentropy_buckets
            SET hash = ?, event_count = ?, updated_at = ?
            WHERE recorded_by = ? AND level = ? AND prefix = ?
            """,
            update_batch,
        )

    if delete_batch:
        safedb.executemany(
            """
            DELETE FROM negentropy_buckets
            WHERE recorded_by = ? AND level = ? AND prefix = ?
            """,
            delete_batch,
        )


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
    defer_buckets: bool = False
) -> None:
    """Add multiple events to the sync system efficiently.

    For large batches, set defer_buckets=True and call rebuild_buckets_for_peer()
    once after all events are added.

    Args:
        db: Database connection
        recorded_by: Peer ID
        events: List of (event_id, created_at) tuples
        defer_buckets: If True, skip bucket updates (caller must call rebuild_buckets_for_peer).
                       If False (default), apply bucket deltas incrementally.
    """
    if not events:
        return

    # Build batch data for insert and cache unified keys
    batch_data: list[tuple[str, str, str, int]] = []
    seen_event_ids: set[str] = set()
    for event_id, created_at in events:
        if event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        unified_key = compute_unified_key(event_id, created_at)
        batch_data.append((recorded_by, event_id, unified_key, created_at))

    # Batch insert and get inserted rows for bucket deltas
    if not defer_buckets:
        try:
            inserted_rows = _insert_negentropy_events_returning(db, recorded_by, batch_data)
        except sqlite3.OperationalError:
            db._conn.executemany("""
                INSERT OR IGNORE INTO negentropy_events
                (recorded_by, event_id, unified_key, created_at)
                VALUES (?, ?, ?, ?)
            """, batch_data)
            event_ids = [event_id for _recorded_by, event_id, _unified_key, _created_at in batch_data]
            existing_event_ids = _fetch_existing_event_ids(db, recorded_by, event_ids)
            inserted_rows = [
                (event_id, unified_key)
                for _recorded_by, event_id, unified_key, _created_at in batch_data
                if event_id not in existing_event_ids
            ]
        _apply_bucket_deltas(db, recorded_by, inserted_rows)
        return

    # Deferred path: insert only
    db._conn.executemany("""
        INSERT OR IGNORE INTO negentropy_events
        (recorded_by, event_id, unified_key, created_at)
        VALUES (?, ?, ?, ?)
    """, batch_data)


def remove_events_from_sync_batch(
    db,
    recorded_by: str,
    event_ids: list[str],
) -> int:
    """Remove events from the sync system (negentropy tables only).

    Deletes from negentropy_events and updates bucket hashes/counts.
    Returns the number of events removed.
    """
    if not event_ids:
        return 0

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unique_ids = list(dict.fromkeys(event_ids))

    removed_rows: list[tuple[str, str]] = []
    chunk_size = int(os.getenv("NEGENTROPY_EVENT_CHUNK", "400"))

    for chunk in _chunked(unique_ids, chunk_size):
        placeholders = ",".join("?" for _ in chunk)
        rows = safedb.query(
            f"SELECT event_id, unified_key FROM negentropy_events "
            f"WHERE recorded_by = ? AND event_id IN ({placeholders})",
            tuple([recorded_by, *chunk]),
        )
        for row in rows:
            removed_rows.append((row["event_id"], row["unified_key"]))

    if not removed_rows:
        return 0

    _apply_bucket_removals(db, recorded_by, removed_rows)

    for chunk in _chunked([row[0] for row in removed_rows], chunk_size):
        placeholders = ",".join("?" for _ in chunk)
        safedb.execute(
            f"DELETE FROM negentropy_events WHERE recorded_by = ? AND event_id IN ({placeholders})",
            tuple([recorded_by, *chunk]),
        )

    return len(removed_rows)


def rebuild_buckets_for_peer(db, recorded_by: str) -> None:
    """Rebuild all bucket hashes for a peer from scratch.

    Efficiently computes XOR fingerprints for all events in one pass.
    This is O(n) in events + O(b) in buckets, much faster than
    incremental updates for large batches.

    Call this after add_events_to_sync_batch() when defer_buckets=True.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)

    # Load all events for this peer
    rows = safedb.query("""
        SELECT event_id, unified_key FROM negentropy_events
        WHERE recorded_by = ?
    """, (recorded_by,))

    # Accumulate XOR fingerprints per bucket in memory
    bucket_xors: dict[tuple[str, str], bytes] = {}
    bucket_counts: dict[tuple[str, str], int] = {}

    for row in rows:
        event_id = row['event_id']
        unified_key = row['unified_key']
        fingerprint = compute_fingerprint(event_id)

        for level in LEVELS:
            prefix_len = LEVEL_PREFIX_LEN[level]
            prefix = unified_key[:prefix_len]
            bucket_key = (level, prefix)

            if bucket_key in bucket_xors:
                bucket_xors[bucket_key] = xor_bytes(bucket_xors[bucket_key], fingerprint)
                bucket_counts[bucket_key] += 1
            else:
                bucket_xors[bucket_key] = fingerprint
                bucket_counts[bucket_key] = 1

    # Clear existing buckets and write new ones in batch
    db._conn.execute("DELETE FROM negentropy_buckets WHERE recorded_by = ?", (recorded_by,))

    # Build batch data for executemany
    bucket_batch = [
        (recorded_by, level, prefix, xor_hash, bucket_counts[(level, prefix)], now)
        for (level, prefix), xor_hash in bucket_xors.items()
    ]

    db._conn.executemany("""
        INSERT INTO negentropy_buckets
        (recorded_by, level, prefix, hash, event_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, bucket_batch)


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
        defer_buckets: If True, skip bucket computation (caller must call
                       rebuild_buckets_for_peer after all events added)
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


def get_bucket_hash(
    db,
    recorded_by: str,
    level: str,
    prefix: str
) -> bytes:
    """Get hash for a bucket.

    With XOR fingerprinting, hashes are always current - just read from DB.
    Returns empty bytes if bucket doesn't exist.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    row = safedb.query_one("""
        SELECT hash FROM negentropy_buckets
        WHERE recorded_by = ? AND level = ? AND prefix = ?
    """, (recorded_by, level, prefix))

    if row and row['hash']:
        return row['hash']
    return b''


# Alias for backwards compatibility
recompute_bucket_hash = get_bucket_hash


def get_hashes_at_level(
    db,
    recorded_by: str,
    level: str,
    parent_prefix: Optional[str] = None
) -> dict[str, bytes]:
    """Get all bucket hashes at a level, optionally filtered by parent prefix.

    With XOR fingerprinting, hashes are always current - just read from DB.

    Returns:
        Dict mapping prefix -> hash (excludes empty buckets)
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    level_prefix_len = LEVEL_PREFIX_LEN[level]

    if parent_prefix is not None:
        # Children with matching prefix start
        prefix_pattern = parent_prefix + '%'
        rows = safedb.query("""
            SELECT prefix, hash FROM negentropy_buckets
            WHERE recorded_by = ? AND level = ?
            AND prefix LIKE ? AND length(prefix) = ?
            AND hash IS NOT NULL
        """, (recorded_by, level, prefix_pattern, level_prefix_len))
    else:
        # All buckets at this level
        rows = safedb.query("""
            SELECT prefix, hash FROM negentropy_buckets
            WHERE recorded_by = ? AND level = ?
            AND hash IS NOT NULL
        """, (recorded_by, level))

    return {row['prefix']: row['hash'] for row in rows}


def get_events_in_bucket(
    db,
    recorded_by: str,
    prefix: str,
    level: str = 'prefix_6'
) -> list[str]:
    """Get all event IDs in a bucket at any level.

    Returns all events whose unified_key starts with the given prefix.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    if level == 'root' or prefix == '':
        # Root - all events
        rows = safedb.query("""
            SELECT event_id FROM negentropy_events
            WHERE recorded_by = ?
            ORDER BY event_id
        """, (recorded_by,))
    else:
        # Prefix match
        prefix_pattern = prefix + '%'
        rows = safedb.query("""
            SELECT event_id FROM negentropy_events
            WHERE recorded_by = ? AND unified_key LIKE ?
            ORDER BY event_id
        """, (recorded_by, prefix_pattern))

    return [r['event_id'] for r in rows]


def get_event_count_in_bucket(
    db,
    recorded_by: str,
    prefix: str,
    level: str
) -> int:
    """Get count of events in a bucket at any level.

    Used to decide whether to drill down or send events.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    if level == 'root' or prefix == '':
        row = safedb.query_one("""
            SELECT COUNT(*) as cnt FROM negentropy_events
            WHERE recorded_by = ?
        """, (recorded_by,))
    else:
        prefix_pattern = prefix + '%'
        row = safedb.query_one("""
            SELECT COUNT(*) as cnt FROM negentropy_events
            WHERE recorded_by = ? AND unified_key LIKE ?
        """, (recorded_by, prefix_pattern))

    return row['cnt'] if row else 0


# ============================================================================
# Adaptive Splitting - Range-based query functions
# ============================================================================

def get_events_in_range(
    db,
    recorded_by: str,
    prefix: str,
    lo_key: str | None = None,
    hi_key: str | None = None,
) -> list[str]:
    """Get all event IDs in an adaptive range.

    Returns events whose unified_key:
    - Starts with prefix
    - Is >= lo_key (if provided)
    - Is < hi_key (if provided)

    Args:
        db: Database connection
        recorded_by: Peer ID
        prefix: Time prefix (6 hex chars for prefix_6)
        lo_key: Optional lower bound (inclusive)
        hi_key: Optional upper bound (exclusive)

    Returns:
        List of event IDs ordered by unified_key
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    conditions = ["recorded_by = ?"]
    params: list[Any] = [recorded_by]

    if prefix:
        conditions.append("unified_key LIKE ?")
        params.append(prefix + '%')

    if lo_key:
        conditions.append("unified_key >= ?")
        params.append(lo_key)

    if hi_key:
        conditions.append("unified_key < ?")
        params.append(hi_key)

    sql = f"""
        SELECT event_id FROM negentropy_events
        WHERE {' AND '.join(conditions)}
        ORDER BY unified_key
    """
    rows = safedb.query(sql, tuple(params))
    return [r['event_id'] for r in rows]


def get_event_count_in_range(
    db,
    recorded_by: str,
    prefix: str,
    lo_key: str | None = None,
    hi_key: str | None = None,
) -> int:
    """Get count of events in an adaptive range."""
    safedb = create_safe_db(db, recorded_by=recorded_by)

    conditions = ["recorded_by = ?"]
    params: list[Any] = [recorded_by]

    if prefix:
        conditions.append("unified_key LIKE ?")
        params.append(prefix + '%')

    if lo_key:
        conditions.append("unified_key >= ?")
        params.append(lo_key)

    if hi_key:
        conditions.append("unified_key < ?")
        params.append(hi_key)

    sql = f"""
        SELECT COUNT(*) as cnt FROM negentropy_events
        WHERE {' AND '.join(conditions)}
    """
    row = safedb.query_one(sql, tuple(params))
    return row['cnt'] if row else 0


def compute_range_hash(
    db,
    recorded_by: str,
    prefix: str,
    lo_key: str | None = None,
    hi_key: str | None = None,
) -> bytes:
    """Compute XOR fingerprint hash for an adaptive range.

    For adaptive ranges, we compute the hash on-demand by XORing
    fingerprints of all events in the range.
    """
    event_ids = get_events_in_range(db, recorded_by, prefix, lo_key, hi_key)
    if not event_ids:
        return b''

    result = compute_fingerprint(event_ids[0])
    for event_id in event_ids[1:]:
        result = xor_bytes(result, compute_fingerprint(event_id))
    return result


def find_split_point(
    db,
    recorded_by: str,
    prefix: str,
    lo_key: str | None = None,
    hi_key: str | None = None,
) -> str | None:
    """Find the median unified_key to split an adaptive range.

    Returns the median key, which becomes the boundary between
    two child ranges: [lo_key, median) and [median, hi_key).
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    conditions = ["recorded_by = ?"]
    params: list[Any] = [recorded_by]

    if prefix:
        conditions.append("unified_key LIKE ?")
        params.append(prefix + '%')

    if lo_key:
        conditions.append("unified_key >= ?")
        params.append(lo_key)

    if hi_key:
        conditions.append("unified_key < ?")
        params.append(hi_key)

    # Get count first
    count_sql = f"""
        SELECT COUNT(*) as cnt FROM negentropy_events
        WHERE {' AND '.join(conditions)}
    """
    count_row = safedb.query_one(count_sql, tuple(params))
    count = count_row['cnt'] if count_row else 0

    if count <= 1:
        return None

    # Get median using OFFSET
    median_offset = count // 2
    sql = f"""
        SELECT unified_key FROM negentropy_events
        WHERE {' AND '.join(conditions)}
        ORDER BY unified_key
        LIMIT 1 OFFSET ?
    """
    row = safedb.query_one(sql, tuple(params) + (median_offset,))
    return row['unified_key'] if row else None


def get_total_event_count(db, recorded_by: str) -> int:
    """Get total number of events being synced."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one("""
        SELECT COUNT(*) as cnt FROM negentropy_events
        WHERE recorded_by = ?
    """, (recorded_by,))
    return row['cnt'] if row else 0


def get_root_hash(db, recorded_by: str) -> bytes:
    """Get the root hash for this peer's event set."""
    return recompute_bucket_hash(db, recorded_by, 'root', '')


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

    Returns a root-level range request to start sync.
    Includes root_hash and total_events for progress tracking.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get root hash
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    if not root_hash:
        # No events, nothing to sync
        return []

    range_id = generate_range_id()

    # Record sync state
    safedb.execute("""
        INSERT INTO negentropy_sync_state
        (recorded_by, connection_id, range_id, level, prefix,
         our_hash, their_hash, status, created_at, updated_at)
        VALUES (?, ?, ?, 'root', '', ?, NULL, 'pending', ?, ?)
    """, (recorded_by, connection_id, range_id, root_hash, t_ms, t_ms))

    return [{
        'type': 'range_request',
        'range_id': range_id,
        'level': 'root',
        'prefix': '',
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
    """Handle an incoming range request.

    Compares our hash with theirs and returns appropriate response.
    Uses adaptive splitting at prefix_6 level for large buckets.
    All responses include root_hash and total_events for progress tracking.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    range_id = msg['range_id']
    level = msg['level']
    prefix = msg.get('prefix', '')
    their_hash = bytes.fromhex(msg['hash']) if msg['hash'] else b''
    lo_key = msg.get('lo_key')
    hi_key = msg.get('hi_key')

    # Get root hash and total events for all responses
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    # Compute our hash for this range
    if level == 'adaptive':
        # Adaptive range: compute hash for lo_key/hi_key bounds
        our_hash = compute_range_hash(db, recorded_by, prefix, lo_key, hi_key)
        event_count = get_event_count_in_range(db, recorded_by, prefix, lo_key, hi_key)
    else:
        # Prefix-based bucket
        our_hash = recompute_bucket_hash(db, recorded_by, level, prefix)
        event_count = get_event_count_in_bucket(db, recorded_by, prefix, level)

    log.info(f"negentropy.handle_range_request: level={level} prefix={prefix} lo={lo_key} hi={hi_key} count={event_count} our_hash={our_hash.hex()[:16] if our_hash else 'empty'} their_hash={their_hash.hex()[:16] if their_hash else 'empty'}")

    # Record the range
    safedb.execute("""
        INSERT INTO negentropy_sync_state
        (recorded_by, connection_id, range_id, level, prefix,
         our_hash, their_hash, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT (recorded_by, connection_id, range_id) DO UPDATE SET
            their_hash = excluded.their_hash,
            updated_at = excluded.updated_at
    """, (recorded_by, connection_id, range_id, level, prefix, our_hash, their_hash, t_ms, t_ms))

    # Check for checkpoint: if their root hash matches ours
    their_root_hash = bytes.fromhex(msg.get('root_hash', '')) if msg.get('root_hash') else None
    if their_root_hash and their_root_hash == root_hash:
        _log_checkpoint(db, recorded_by, connection_id, root_hash, t_ms)

    responses = []

    if our_hash == their_hash:
        # Hashes match - this range is synced!
        safedb.execute("""
            UPDATE negentropy_sync_state
            SET status = 'matched', updated_at = ?
            WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
        """, (t_ms, recorded_by, connection_id, range_id))

        responses.append({
            'type': 'range_matched',
            'range_id': range_id,
            'root_hash': root_hash.hex() if root_hash else '',
            'total_events': total_events,
        })

    elif event_count <= EVENTS_THRESHOLD:
        # Bucket/range has few enough events - send blobs directly
        safedb.execute("""
            UPDATE negentropy_sync_state
            SET status = 'events_sent', updated_at = ?
            WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
        """, (t_ms, recorded_by, connection_id, range_id))

        if level == 'adaptive':
            event_ids = get_events_in_range(db, recorded_by, prefix, lo_key, hi_key)
        else:
            event_ids = get_events_in_bucket(db, recorded_by, prefix, level)

        # Send actual event blobs - they'll dedupe on their side
        if event_ids:
            sent = _send_event_blobs(db, recorded_by, connection_id, event_ids, t_ms)
            log.info(f"negentropy: sent {sent} event blobs at {level} level ({event_count} events)")

        # Limit event_ids in wire message (blobs already sent)
        wire_event_ids = event_ids[:EVENT_ID_MAX] if len(event_ids) > EVENT_ID_MAX else event_ids

        responses.append({
            'type': 'range_events',
            'range_id': range_id,
            'prefix': prefix,
            'event_ids': wire_event_ids,
            'our_hash': our_hash.hex() if our_hash else '',
            'root_hash': root_hash.hex() if root_hash else '',
            'total_events': total_events,
        })

    elif level == 'prefix_6' or level == 'adaptive':
        # At prefix_6 or adaptive with many events: use adaptive binary split
        safedb.execute("""
            UPDATE negentropy_sync_state
            SET status = 'diverged', updated_at = ?
            WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
        """, (t_ms, recorded_by, connection_id, range_id))

        # Find median to split on
        split_key = find_split_point(db, recorded_by, prefix, lo_key, hi_key)
        if not split_key:
            # Can't split further, send what we have
            event_ids = get_events_in_range(db, recorded_by, prefix, lo_key, hi_key)
            if event_ids:
                sent = _send_event_blobs(db, recorded_by, connection_id, event_ids, t_ms)
                log.info(f"negentropy: sent {sent} blobs (no split point)")
            return responses

        # Create two child ranges: [lo, split) and [split, hi)
        for child_lo, child_hi in [(lo_key, split_key), (split_key, hi_key)]:
            child_hash = compute_range_hash(db, recorded_by, prefix, child_lo, child_hi)
            if not child_hash:
                continue  # Empty range

            child_range_id = generate_range_id()
            safedb.execute("""
                INSERT INTO negentropy_sync_state
                (recorded_by, connection_id, range_id, level, prefix,
                 our_hash, their_hash, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)
            """, (recorded_by, connection_id, child_range_id, 'adaptive', prefix, child_hash, t_ms, t_ms))

            responses.append({
                'type': 'range_request',
                'range_id': child_range_id,
                'level': 'adaptive',
                'prefix': prefix,
                'lo_key': child_lo,
                'hi_key': child_hi,
                'hash': child_hash.hex() if child_hash else '',
                'parent_range_id': range_id,
                'root_hash': root_hash.hex() if root_hash else '',
                'total_events': total_events,
            })

    else:
        # Drill down: send child prefix hashes (root → prefix_2 → prefix_4 → prefix_6)
        child_level = get_child_level(level)
        child_hashes = get_hashes_at_level(db, recorded_by, child_level, prefix)

        safedb.execute("""
            UPDATE negentropy_sync_state
            SET status = 'diverged', updated_at = ?
            WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
        """, (t_ms, recorded_by, connection_id, range_id))

        # Create child ranges
        for child_prefix, child_hash in child_hashes.items():
            child_range_id = generate_range_id()

            safedb.execute("""
                INSERT INTO negentropy_sync_state
                (recorded_by, connection_id, range_id, level, prefix,
                 our_hash, their_hash, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)
            """, (recorded_by, connection_id, child_range_id, child_level, child_prefix, child_hash, t_ms, t_ms))

            responses.append({
                'type': 'range_request',
                'range_id': child_range_id,
                'level': child_level,
                'prefix': child_prefix,
                'hash': child_hash.hex() if child_hash else '',
                'parent_range_id': range_id,
                'root_hash': root_hash.hex() if root_hash else '',
                'total_events': total_events,
            })

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

    # Track for congestion control - we received a response
    _cc_on_response(recorded_by, connection_id, t_ms)

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


def handle_range_events(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int
) -> list[dict]:
    """Handle incoming events for a bucket.

    Sends actual event blobs for events they need.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Track for congestion control - we received a response
    _cc_on_response(recorded_by, connection_id, t_ms)

    range_id = msg['range_id']
    prefix = msg.get('prefix', '')
    their_event_ids = set(msg['event_ids'])

    # Get root hash and total events for responses
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    # Check for checkpoint
    their_root_hash = bytes.fromhex(msg.get('root_hash', '')) if msg.get('root_hash') else None
    if their_root_hash and their_root_hash == root_hash:
        _log_checkpoint(db, recorded_by, connection_id, root_hash, t_ms)

    # Get our events for this bucket
    our_event_ids = set(get_events_in_bucket(db, recorded_by, prefix))

    # Events they have that we don't -> we need to request/store
    events_we_need = their_event_ids - our_event_ids

    # Events we have that they don't -> send blobs
    events_they_need = our_event_ids - their_event_ids

    # Mark range complete
    safedb.execute("""
        UPDATE negentropy_sync_state
        SET status = 'complete', updated_at = ?
        WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
    """, (t_ms, recorded_by, connection_id, range_id))

    # Send actual event blobs they need
    if events_they_need:
        sent = _send_event_blobs(db, recorded_by, connection_id, list(events_they_need), t_ms)
        log.info(f"negentropy: sent {sent} event blobs to connection {connection_id[:20]}...")

    # No protocol response needed - we sent the blobs directly
    return []


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
    elif msg_type == 'range_events':
        return handle_range_events(db, recorded_by, connection_id, msg, t_ms)
    else:
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
            # Track for congestion control
            _cc_on_send(recorded_by, conn.key_id, t_ms)
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

            # Check for CC timeout first (shrinks window if needed)
            _cc_check_timeout(peer_id, conn.key_id, t_ms)

            # Congestion control: only send if window allows
            if not _cc_can_send(peer_id, conn.key_id):
                log.debug(f"CC: skipping conn={conn.key_id[:16]}... (window full)")
                continue

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
