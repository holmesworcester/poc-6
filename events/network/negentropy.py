"""
Negentropy-style deterministic sync protocol - TIME-BASED BUCKETS variant.

This variant uses time-based bucketing with a fixed epoch. The unified key is:
  (relative_minutes << 16) | hash_16bits

Where:
- relative_minutes: 32-bit value representing minutes since EPOCH_MS (2025-01-01 UTC)
- hash_16bits: 16-bit BLAKE2b hash of event_id for disambiguation

This provides temporal clustering (events at the same minute are adjacent) while
still allowing 65536 events per minute to be distinguished.

Design goals:
- Deterministic: no false positives, exact set reconciliation
- Finality: clear "done" state when synced (root hashes match)
- UI-inspectable: track state per connection for visibility
- Connection-scoped: ranges tracked by connection_id
- Peer-consistent: same event_id + created_at always produces same unified_key
- Efficient bisection: ~14 rounds for a 1-week network vs ~47 for unbounded

Key format (48 bits = 12 hex chars):
- Bits 47-16: relative minutes from epoch (32 bits, ~8000 years range)
- Bits 15-0: hash suffix (16 bits, 65536 events/minute capacity)

Bucket hierarchy (by prefix):
- root: all events
- prefix_2: first 2 hex chars (256 buckets)
- prefix_4: first 4 hex chars (65536 buckets)
- prefix_6: first 6 hex chars (16.7M buckets)

Why fixed epoch (2025-01-01 UTC):
- No lookup needed - works for newcomers immediately
- Deterministic across all peers
- No chicken-and-egg problem

Why minutes (not milliseconds):
- Each bisection depth spans a few minutes of events
- 65536 events per minute capacity (handles 1GB file bursts)
- 32 bits of minutes = 8171 years from epoch

Protocol flow:
1. On new connection: send root hash
2. Receive their hash, compare ranges
3. For mismatched ranges: drill down by prefix until bucket has ≤EVENTS_THRESHOLD events
4. At threshold: send actual event IDs
5. When root hashes match: sync complete for this connection
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


# Fixed epoch for relative timestamp computation - 2025-01-01 00:00:00 UTC
# Using a hardcoded constant avoids chicken-and-egg problems for newcomers
# who don't yet know the network's created_at timestamp.
EPOCH_MS = 1735689600000  # 2025-01-01 00:00:00 UTC

# Maximum unified key value (48 bits: 32 bits minutes + 16 bits hash)
# 32 bits of minutes = 8171 years from epoch, plenty of range
MAX_UNIFIED_KEY = (1 << 48) - 1


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
LEVEL_PREFIX_8 = 4
LEVEL_PREFIX_10 = 5

# Negentropy wire format sizes
RANGE_ID_SIZE = 8
PREFIX_BYTES = 5  # 5 bytes = 10 hex chars for prefix_10
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
    level: int,
    prefix_bytes: bytes,
    hash_bytes: bytes,
    root_hash: bytes,
    total_events: int,
    parent_range_id: bytes,
    event_ids: list[bytes],
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
    - event_count (1)
    - event_ids (15 * 16 = 240)
    """
    _require_len("connection_id", connection_id, 16)
    _require_len("reply_connection_id", reply_connection_id, 16)
    if msg_type not in (MSG_RANGE_REQUEST, MSG_RANGE_MATCHED, MSG_RANGE_EVENTS):
        raise ValueError("invalid negentropy msg_type")
    _require_len("range_id", range_id, RANGE_ID_SIZE)
    if level not in (LEVEL_ROOT, LEVEL_PREFIX_2, LEVEL_PREFIX_4, LEVEL_PREFIX_6):
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
    payload[90] = len(event_ids)
    cursor = 91
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
    event_count = data[90]
    if event_count > EVENT_ID_MAX:
        raise ValueError("negentropy event_count exceeds max")
    event_ids_list: list[bytes] = []
    cursor = 91
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
LEVELS = ['root', 'prefix_2', 'prefix_4', 'prefix_6', 'prefix_8', 'prefix_10']

# Hex characters per level
LEVEL_PREFIX_LEN = {
    'root': 0,
    'prefix_2': 2,     # 256 buckets (coarse time)
    'prefix_4': 4,     # 65,536 buckets (medium time)
    'prefix_6': 6,     # 16.7M buckets (fine time, ~4.4 min)
    'prefix_8': 8,     # time + 2 hash chars (256 sub-buckets per time)
    'prefix_10': 10,   # time + 4 hash chars (65,536 sub-buckets per time)
}

# When bucket has this many events or fewer, send event IDs instead of drilling down
# 200 allows 4x more events per leaf node, reducing sync rounds by ~4x
# Risk: larger messages, more retransmit cost on packet loss
EVENTS_THRESHOLD = 200


# ============================================================================
# Bucket Size Helpers for Range-to-Bucket Mapping
# ============================================================================

def get_bucket_size(level: str) -> int:
    """Get the number of unified keys covered by one bucket at this level.

    Each hex char represents 4 bits, so a prefix of length N covers
    2^(4*(12-N)) = 16^(12-N) unified keys.
    """
    prefix_len = LEVEL_PREFIX_LEN[level]
    return 1 << (4 * (12 - prefix_len))


def find_best_level(range_size: int) -> str:
    """Find the coarsest bucket level where bucket_size <= range_size.

    Returns the level that provides the best balance: coarse enough to use
    few buckets, but fine enough that buckets fit within the range.
    """
    for level in LEVELS[1:]:  # Skip root
        if get_bucket_size(level) <= range_size:
            return level
    return 'prefix_10'


def range_to_prefix(start: int, level: str) -> str:
    """Convert an aligned range start to its bucket prefix.

    Args:
        start: Range start as integer (should be aligned to bucket boundary)
        level: Bucket level (determines prefix length)

    Returns:
        Hex prefix string for this bucket
    """
    hex_str = f"{start:012x}"
    prefix_len = LEVEL_PREFIX_LEN[level]
    return hex_str[:prefix_len]


# ============================================================================
# Congestion Control State
# ============================================================================
# Per-connection adaptive windowing based on RTT measurement.
# See docs/quiet-protocol-specification.md "Congestion control" section.

@dataclass
class CCState:
    """Congestion control state for a single connection.

    Uses range-ID tracking for accurate RTT measurement and in-flight counting.
    Each outgoing request gets a unique ID; responses echo the ID back.
    Supports both string range_ids (bucket protocol) and int range_ids (bisection protocol).
    """
    window: int = 4                          # Max in-flight requests (CC_INITIAL_WINDOW)
    rtt_ms: float = 200.0                    # RTT estimate (exponential moving average)
    next_id: int = 1                         # Next range_id to assign
    outstanding: dict = None                 # {range_id (str or int): send_time_ms}

    def __post_init__(self):
        if self.outstanding is None:
            self.outstanding = {}

    @property
    def in_flight(self) -> int:
        return len(self.outstanding)


# Constants for congestion control
CC_MIN_WINDOW = 1
CC_INITIAL_WINDOW = 4            # Start with 4 parallel requests
CC_MAX_WINDOW = 64               # Allow up to 64 parallel requests
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


def _cc_allocate_id(recorded_by: str, connection_id: str, t_ms: int) -> int:
    """Allocate a range_id for a new request and track send time."""
    state = _get_cc_state(recorded_by, connection_id)
    range_id = state.next_id
    state.next_id += 1
    state.outstanding[range_id] = t_ms
    return range_id


def _cc_track_request(recorded_by: str, connection_id: str, range_id: str, t_ms: int) -> None:
    """Track a request by its range_id (bucket protocol - string IDs)."""
    state = _get_cc_state(recorded_by, connection_id)
    state.outstanding[range_id] = t_ms


def _cc_on_send(recorded_by: str, connection_id: str, t_ms: int) -> None:
    """Record that we sent a range request (legacy, for non-ID-tracked sends)."""
    # For backwards compatibility - allocates an ID but discards it
    _cc_allocate_id(recorded_by, connection_id, t_ms)


def _cc_on_response(recorded_by: str, connection_id: str, t_ms: int, range_id = None) -> None:
    """Record that we received a response.

    If range_id is provided (str or int), uses precise RTT measurement.
    Otherwise falls back to approximate tracking.
    """
    state = _get_cc_state(recorded_by, connection_id)

    if range_id is not None and range_id in state.outstanding:
        # Precise tracking: measure RTT from this specific request
        send_time = state.outstanding.pop(range_id)
        rtt_sample = t_ms - send_time
        if rtt_sample > 0:
            state.rtt_ms = (1 - CC_RTT_ALPHA) * state.rtt_ms + CC_RTT_ALPHA * rtt_sample

        # Grow window on successful response (additive increase)
        state.window = min(state.window + 1, CC_MAX_WINDOW)
        log.debug(f"CC: conn={connection_id[:16]}... RTT={rtt_sample}ms (avg={state.rtt_ms:.0f}ms) window={state.window} in_flight={state.in_flight}")

    elif range_id is None:
        # Legacy: no ID tracking, remove oldest if any
        if state.outstanding:
            # Find oldest by send_time, not by key (keys can be str or int)
            oldest_id = min(state.outstanding.keys(), key=lambda k: state.outstanding[k])
            send_time = state.outstanding.pop(oldest_id)
            rtt_sample = t_ms - send_time
            if rtt_sample > 0:
                state.rtt_ms = (1 - CC_RTT_ALPHA) * state.rtt_ms + CC_RTT_ALPHA * rtt_sample
            state.window = min(state.window + 1, CC_MAX_WINDOW)


def _cc_check_timeout(recorded_by: str, connection_id: str, t_ms: int) -> bool:
    """Check for timeout and shrink window if needed. Returns True if timed out."""
    state = _get_cc_state(recorded_by, connection_id)
    if not state.outstanding:
        return False

    timeout_ms = CC_TIMEOUT_MULTIPLIER * state.rtt_ms
    oldest_time = min(state.outstanding.values()) if state.outstanding else t_ms

    if (t_ms - oldest_time) > timeout_ms:
        # Timeout - shrink window and clear outstanding
        old_window = state.window
        state.window = max(CC_MIN_WINDOW, state.window // 2)
        state.outstanding.clear()
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
    """Compute unified key: (relative_minutes << 16) | hash_16bits.

    Uses minutes from fixed epoch (2025-01-01) for tight temporal clustering.
    Each bisection depth spans a few minutes of events.
    32 bits for relative minutes = 8000+ years range.
    16 bits for hash = 65536 events per minute capacity.

    Args:
        event_id: The event identifier
        created_at: Event creation timestamp in milliseconds

    Returns:
        12 character hex string (48 bits: 32 bits minutes + 16 bits hash)
    """
    # Compute relative minutes from epoch (clamped to 32 bits)
    relative_min = max(0, (created_at - EPOCH_MS) // 60_000)
    relative_min = min(relative_min, 0xFFFFFFFF)  # clamp to 32 bits

    # Compute 16-bit hash for disambiguation within the same minute
    h = hashlib.blake2b(event_id.encode('utf-8'), digest_size=2).digest()
    hash_bits = int.from_bytes(h, 'big')

    # Combine: (relative_minutes << 16) | hash_16bits
    unified_key = (relative_min << 16) | hash_bits

    return f"{unified_key:012x}"  # 12 hex chars (48 bits)


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
# Binary Bisection Functions
# ============================================================================

# Range constants (48-bit unified key space)
RANGE_MIN = 0
RANGE_MAX = (1 << 48)  # Exclusive end (one past max valid key)


def range_to_hex(value: int) -> str:
    """Convert range value to hex string.

    Uses 12 chars for values < 2^48, 13 chars for 2^48 (exclusive end).
    """
    if value >= RANGE_MAX:
        return f"{value:013x}"
    return f"{value:012x}"


def hex_to_range(hex_str: str) -> int:
    """Convert hex string to range value."""
    return int(hex_str, 16)


def range_midpoint(start: int, end: int) -> int:
    """Compute midpoint of a range for binary bisection."""
    return start + (end - start) // 2


def _scan_events_hash(
    db,
    recorded_by: str,
    start: int,
    end: int,
) -> bytes:
    """Scan events in range [start, end) and compute XOR fingerprint hash.

    This is the fallback O(n) implementation for unaligned edge ranges.
    For aligned ranges, use compute_range_hash() which uses bucket cache.

    Args:
        db: Database connection
        recorded_by: Peer ID
        start: Range start (inclusive) as integer
        end: Range end (exclusive) as integer

    Returns:
        16-byte XOR fingerprint, or ZERO_HASH if no events in range
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    start_hex = range_to_hex(start)
    end_hex = range_to_hex(end)

    # Query events with unified_key in [start_hex, end_hex)
    rows = safedb.query("""
        SELECT event_id FROM negentropy_events
        WHERE recorded_by = ? AND unified_key >= ? AND unified_key < ?
    """, (recorded_by, start_hex, end_hex))

    if not rows:
        return ZERO_HASH

    # XOR all fingerprints
    result = ZERO_HASH
    for row in rows:
        fp = compute_fingerprint(row['event_id'])
        result = xor_bytes(result, fp)

    return result


def _scan_events_count(
    db,
    recorded_by: str,
    start: int,
    end: int,
) -> int:
    """Scan events in range [start, end) and return count.

    This is the fallback O(n) implementation for unaligned edge ranges.
    For aligned ranges, use count_events_in_range() which uses bucket cache.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    start_hex = range_to_hex(start)
    end_hex = range_to_hex(end)

    row = safedb.query_one("""
        SELECT COUNT(*) as cnt FROM negentropy_events
        WHERE recorded_by = ? AND unified_key >= ? AND unified_key < ?
    """, (recorded_by, start_hex, end_hex))

    return row['cnt'] if row else 0


def compute_range_hash(
    db,
    recorded_by: str,
    start: int,
    end: int,
) -> bytes:
    """Compute XOR fingerprint hash for all events in range [start, end).

    Uses bucket cache for O(buckets) performance instead of O(events).
    For bucket-aligned ranges, XORs cached bucket hashes.
    For edges, recursively uses finer bucket levels.

    Args:
        db: Database connection
        recorded_by: Peer ID
        start: Range start (inclusive) as integer
        end: Range end (exclusive) as integer

    Returns:
        16-byte XOR fingerprint, or ZERO_HASH if no events in range
    """
    if start >= end:
        return ZERO_HASH

    # Full range = root hash (O(1) lookup)
    if start == 0 and end >= RANGE_MAX:
        root = get_bucket_hash(db, recorded_by, 'root', '')
        return root if root else ZERO_HASH

    range_size = end - start
    level = find_best_level(range_size)
    bucket_size = get_bucket_size(level)

    # If range is smaller than smallest bucket (prefix_10 = 2^8 = 256), just scan
    if range_size < bucket_size:
        return _scan_events_hash(db, recorded_by, start, end)

    # Align boundaries to bucket boundaries
    aligned_start = ((start + bucket_size - 1) // bucket_size) * bucket_size
    aligned_end = (end // bucket_size) * bucket_size

    result = ZERO_HASH

    # Left edge: recursively compute hash [start, aligned_start)
    if start < aligned_start:
        result = xor_bytes(result, compute_range_hash(db, recorded_by, start, aligned_start))

    # Middle: XOR bucket hashes [aligned_start, aligned_end)
    if aligned_start < aligned_end:
        prefixes = []
        current = aligned_start
        while current < aligned_end:
            prefixes.append(range_to_prefix(current, level))
            current += bucket_size

        bucket_hashes = _fetch_bucket_hashes_batch(db, recorded_by, level, prefixes)
        for h in bucket_hashes:
            if h:
                result = xor_bytes(result, h)

    # Right edge: recursively compute hash [aligned_end, end)
    if aligned_end < end:
        result = xor_bytes(result, compute_range_hash(db, recorded_by, aligned_end, end))

    return result


def count_events_in_range(
    db,
    recorded_by: str,
    start: int,
    end: int,
) -> int:
    """Count events in range [start, end).

    Uses bucket cache for O(buckets) performance instead of O(events).
    For bucket-aligned ranges, sums cached bucket counts.
    For edges, recursively uses finer bucket levels.
    """
    if start >= end:
        return 0

    # Full range = root bucket count (O(1) lookup)
    if start == 0 and end >= RANGE_MAX:
        safedb = create_safe_db(db, recorded_by=recorded_by)
        row = safedb.query_one("""
            SELECT event_count FROM negentropy_buckets
            WHERE recorded_by = ? AND level = ? AND prefix = ?
        """, (recorded_by, 'root', ''))
        return row['event_count'] if row and row['event_count'] else 0

    range_size = end - start
    level = find_best_level(range_size)
    bucket_size = get_bucket_size(level)

    # If range is smaller than smallest bucket, just scan
    if range_size < bucket_size:
        return _scan_events_count(db, recorded_by, start, end)

    # Align boundaries to bucket boundaries
    aligned_start = ((start + bucket_size - 1) // bucket_size) * bucket_size
    aligned_end = (end // bucket_size) * bucket_size

    result = 0

    # Left edge: recursively count [start, aligned_start)
    if start < aligned_start:
        result += count_events_in_range(db, recorded_by, start, aligned_start)

    # Middle: sum bucket counts [aligned_start, aligned_end)
    if aligned_start < aligned_end:
        prefixes = []
        current = aligned_start
        while current < aligned_end:
            prefixes.append(range_to_prefix(current, level))
            current += bucket_size

        bucket_counts = _fetch_bucket_counts_batch(db, recorded_by, level, prefixes)
        result += sum(bucket_counts)

    # Right edge: recursively count [aligned_end, end)
    if aligned_end < end:
        result += count_events_in_range(db, recorded_by, aligned_end, end)

    return result


def get_events_in_range(
    db,
    recorded_by: str,
    start: int,
    end: int,
) -> list[str]:
    """Get all event IDs in range [start, end)."""
    safedb = create_safe_db(db, recorded_by=recorded_by)

    start_hex = range_to_hex(start)
    end_hex = range_to_hex(end)

    rows = safedb.query("""
        SELECT event_id FROM negentropy_events
        WHERE recorded_by = ? AND unified_key >= ? AND unified_key < ?
        ORDER BY unified_key
    """, (recorded_by, start_hex, end_hex))

    return [row['event_id'] for row in rows]


def first_leaf_in_range(
    db,
    recorded_by: str,
    start: int,
    end: int,
    threshold: int = EVENTS_THRESHOLD,
) -> tuple[int, int]:
    """Find the first leaf range (count <= threshold) within [start, end).

    Uses binary search to find the smallest prefix range starting at 'start'
    that contains <= threshold events.

    Returns:
        (leaf_start, leaf_end) tuple
    """
    # Start with the full range
    leaf_start = start
    leaf_end = end

    while True:
        count = count_events_in_range(db, recorded_by, leaf_start, leaf_end)

        if count <= threshold:
            # This range is small enough
            return (leaf_start, leaf_end)

        # Too many events - bisect and take left half
        mid = range_midpoint(leaf_start, leaf_end)
        if mid <= leaf_start:
            # Can't bisect further
            return (leaf_start, leaf_end)

        leaf_end = mid


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


def rebuild_negentropy_index(db, recorded_by: str) -> int:
    """Rebuild negentropy index by recomputing all unified keys.

    Call this after schema changes that affect the unified key format.
    Recomputes unified_key for all events using the current compute_unified_key()
    implementation, then rebuilds bucket hashes.

    Args:
        db: Database connection
        recorded_by: Peer ID to rebuild

    Returns:
        Number of events reindexed
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get all events with their created_at timestamps
    rows = safedb.query("""
        SELECT event_id, created_at FROM negentropy_events
        WHERE recorded_by = ?
    """, (recorded_by,))

    # Recompute unified keys
    update_batch = []
    for row in rows:
        event_id = row['event_id']
        created_at = row['created_at'] or 0
        new_key = compute_unified_key(event_id, created_at)
        update_batch.append((new_key, recorded_by, event_id))

    # Batch update unified keys
    if update_batch:
        db._conn.executemany("""
            UPDATE negentropy_events
            SET unified_key = ?
            WHERE recorded_by = ? AND event_id = ?
        """, update_batch)

    # Rebuild bucket hashes
    rebuild_buckets_for_peer(db, recorded_by)

    log.info(f"rebuild_negentropy_index: reindexed {len(update_batch)} events for peer={recorded_by[:20]}...")
    return len(update_batch)


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


def _fetch_bucket_hashes_batch(
    db,
    recorded_by: str,
    level: str,
    prefixes: list[str]
) -> list[bytes | None]:
    """Fetch multiple bucket hashes in one query.

    Args:
        db: Database connection
        recorded_by: Peer ID
        level: Bucket level
        prefixes: List of bucket prefixes to fetch

    Returns:
        List of hashes (or None) in same order as prefixes
    """
    if not prefixes:
        return []
    safedb = create_safe_db(db, recorded_by=recorded_by)
    placeholders = ",".join("?" for _ in prefixes)
    rows = safedb.query(f"""
        SELECT prefix, hash FROM negentropy_buckets
        WHERE recorded_by = ? AND level = ? AND prefix IN ({placeholders})
    """, (recorded_by, level, *prefixes))
    hash_map = {row['prefix']: row['hash'] for row in rows}
    return [hash_map.get(p) for p in prefixes]


def _fetch_bucket_counts_batch(
    db,
    recorded_by: str,
    level: str,
    prefixes: list[str]
) -> list[int]:
    """Fetch multiple bucket event counts in one query.

    Args:
        db: Database connection
        recorded_by: Peer ID
        level: Bucket level
        prefixes: List of bucket prefixes to fetch

    Returns:
        List of event counts in same order as prefixes (0 for missing buckets)
    """
    if not prefixes:
        return []
    safedb = create_safe_db(db, recorded_by=recorded_by)
    placeholders = ",".join("?" for _ in prefixes)
    rows = safedb.query(f"""
        SELECT prefix, event_count FROM negentropy_buckets
        WHERE recorded_by = ? AND level = ? AND prefix IN ({placeholders})
    """, (recorded_by, level, *prefixes))
    count_map = {row['prefix']: row['event_count'] or 0 for row in rows}
    return [count_map.get(p, 0) for p in prefixes]


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
    All responses include root_hash and total_events for progress tracking.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    range_id = msg['range_id']
    level = msg['level']
    prefix = msg.get('prefix', '')
    their_hash = bytes.fromhex(msg['hash']) if msg['hash'] else b''

    # Get root hash and total events for all responses
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    # Compute our hash for this bucket
    our_hash = recompute_bucket_hash(db, recorded_by, level, prefix)
    log.info(f"negentropy.handle_range_request: level={level} prefix={prefix} our_hash={our_hash.hex()[:16] if our_hash else 'empty'} their_hash={their_hash.hex()[:16] if their_hash else 'empty'} match={our_hash == their_hash}")

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

    else:
        # Hashes differ - check if we should send events or drill down
        event_count = get_event_count_in_bucket(db, recorded_by, prefix, level)

        # At finest level OR bucket has few enough events to send directly
        max_events = EVENT_ID_MAX
        if level == 'prefix_6' or event_count <= max_events:
            safedb.execute("""
                UPDATE negentropy_sync_state
                SET status = 'events_sent', updated_at = ?
                WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
            """, (t_ms, recorded_by, connection_id, range_id))

            event_ids = get_events_in_bucket(db, recorded_by, prefix, level)

            # Send actual event blobs - they'll dedupe on their side
            if event_ids:
                sent = _send_event_blobs(db, recorded_by, connection_id, event_ids, t_ms)
                log.info(f"negentropy: sent {sent} event blobs at {level} level ({event_count} events in bucket)")

            # Signal "at leaf" - receiver will send all their events for this prefix
            # No need to send event_ids list; both sides send all, receiver dedupes
            responses.append({
                'type': 'range_events',
                'range_id': range_id,
                'prefix': prefix,
                'event_ids': [],  # Empty - "both send all at leaf" protocol
                'our_hash': our_hash.hex() if our_hash else '',
                'root_hash': root_hash.hex() if root_hash else '',
                'total_events': total_events,
            })

        else:
            # Drill down: send child hashes
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

    # Track for congestion control - we received a response with range_id for precise RTT
    _cc_on_response(recorded_by, connection_id, t_ms, range_id=range_id)

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
    """Handle incoming 'at leaf' signal for a bucket.

    Both sides send all events at leaf - but only if hashes differ.
    If our hash matches their hash, we have the same events (no need to send).
    This avoids echoing back events we just received.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    range_id = msg['range_id']

    # Track for congestion control - we received a response with range_id for precise RTT
    _cc_on_response(recorded_by, connection_id, t_ms, range_id=range_id)
    prefix = msg.get('prefix', '')

    # Get root hash and total events for responses
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    # Check for checkpoint
    their_root_hash = bytes.fromhex(msg.get('root_hash', '')) if msg.get('root_hash') else None
    if their_root_hash and their_root_hash == root_hash:
        _log_checkpoint(db, recorded_by, connection_id, root_hash, t_ms)

    # Mark range complete
    safedb.execute("""
        UPDATE negentropy_sync_state
        SET status = 'complete', updated_at = ?
        WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
    """, (t_ms, recorded_by, connection_id, range_id))

    # Compare our bucket hash with theirs - only send if different
    # This avoids echoing back events we just received from them
    their_bucket_hash = bytes.fromhex(msg.get('our_hash', '')) if msg.get('our_hash') else None
    our_bucket_hash = recompute_bucket_hash(db, recorded_by, 'prefix_6', prefix)

    if their_bucket_hash and our_bucket_hash == their_bucket_hash:
        # Hashes match - we have the same events, nothing to send
        log.debug(f"negentropy: bucket {prefix} hashes match, no events to send")
        return []

    # Hashes differ - send all our events for this bucket
    our_event_ids = list(get_events_in_bucket(db, recorded_by, prefix))
    if our_event_ids:
        sent = _send_event_blobs(db, recorded_by, connection_id, our_event_ids, t_ms)
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
            # Track for congestion control with actual range_id for precise RTT
            if 'range_id' in msg:
                _cc_track_request(recorded_by, conn.key_id, msg['range_id'], t_ms)
            else:
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
            # Track range_request responses for CC (these are our new outgoing requests)
            if response.get('type') == 'range_request' and 'range_id' in response:
                _cc_track_request(recorded_by, connection_id, response['range_id'], t_ms)
    return sent


def sync_all_connections(t_ms: int, db: Any) -> dict:
    """Sync all active connections for all local peers.

    Called by NegentropySyncJob. Iterates all local peers and their
    established connections, sending negentropy root hashes to initiate
    or continue sync.

    Sync is initiated for ALL established connections, even if our root hash
    hasn't changed - the remote peer may have new events we need to pull.

    In STAR topology mode:
    - Clients sync ONLY with the server relay
    - Server relay syncs with everyone

    Returns:
        Stats dict with counts
    """
    from core.db import create_unsafe_db
    from events.network import connection_request as conn_module
    from events.network import server_connection
    from events.identity import network as network_module
    from events.identity.peer_shared import get_self

    unsafedb = create_unsafe_db(db)
    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    total_connections = 0
    total_messages = 0

    for peer_row in local_peers:
        peer_id = peer_row['peer_id']

        # Get our peer_shared_id for star topology filtering
        identity = get_self(peer_id, db)
        our_peer_shared_id = identity.get('peer_shared_id') if identity else None

        # Get network_id for topology check
        network_id = network_module.get_network_id(peer_id, db)

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

            # STAR TOPOLOGY: Filter connections based on network sync mode
            # Skip this connection if star topology says we shouldn't sync with this peer
            if network_id and our_peer_shared_id and conn.peer_shared_id:
                if not server_connection.should_sync_with_peer(
                    our_peer_id=peer_id,
                    our_peer_shared_id=our_peer_shared_id,
                    target_peer_shared_id=conn.peer_shared_id,
                    network_id=network_id,
                    db=db
                ):
                    log.debug(f"Star topology: skipping sync with {conn.peer_shared_id[:16]}... (not server relay)")
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
# Binary Bisection Protocol V2
# ============================================================================
# Stateless ping-pong protocol with binary bisection.
# Each message is self-describing: (start, end, hash)
# No session state needed - range IS the state.

# Message types for binary bisection protocol
MSG_SYNC_RANGE = 4    # Range hash message
MSG_SYNC_MATCH = 5    # Explicit match confirmation


def handle_sync_range(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int,
) -> list[dict]:
    """Handle a sync_range message using binary bisection.

    Protocol rules:
    - their_hash == our_hash: send MATCH
    - their_hash == ZERO: send events from first leaf + next range
    - our_hash == ZERO: send ZERO to request events
    - mismatch: bisect (if large) or send events (if small)

    Args:
        db: Database connection
        recorded_by: Local peer ID
        connection_id: Connection ID
        msg: Message dict with 'start', 'end', 'hash', optional 'range_id'
        t_ms: Current timestamp

    Returns:
        List of response messages
    """
    start = hex_to_range(msg['start'])
    end = hex_to_range(msg['end'])
    their_hash = bytes.fromhex(msg['hash']) if msg.get('hash') else ZERO_HASH
    their_range_id = msg.get('range_id')  # Echo back for CC tracking

    # CC: If this message has a range_id we're tracking, it's a response to our request
    if their_range_id is not None:
        _cc_on_response(recorded_by, connection_id, t_ms, range_id=their_range_id)

    our_hash = compute_range_hash(db, recorded_by, start, end)

    responses = []

    # Helper to add range_id to response (echo theirs, or allocate new if initiating)
    def make_response(resp: dict) -> dict:
        if their_range_id is not None:
            resp['range_id'] = their_range_id
        return resp

    # Case 1: Hashes match
    if their_hash == our_hash:
        responses.append(make_response({
            'type': 'sync_match',
            'start': msg['start'],
            'end': msg['end'],
        }))
        return responses

    # Case 2: They have nothing (ZERO), we have events
    if their_hash == ZERO_HASH and our_hash != ZERO_HASH:
        # Find first leaf and send events
        leaf_start, leaf_end = first_leaf_in_range(db, recorded_by, start, end)
        event_ids = get_events_in_range(db, recorded_by, leaf_start, leaf_end)

        # Send event blobs
        if event_ids:
            _send_event_blobs(db, recorded_by, connection_id, event_ids, t_ms)

        # Send leaf range hash (for verification)
        leaf_hash = compute_range_hash(db, recorded_by, leaf_start, leaf_end)
        responses.append(make_response({
            'type': 'sync_range',
            'start': range_to_hex(leaf_start),
            'end': range_to_hex(leaf_end),
            'hash': leaf_hash.hex(),
        }))

        # Send next range hash (to continue)
        if leaf_end < end:
            next_hash = compute_range_hash(db, recorded_by, leaf_end, end)
            responses.append(make_response({
                'type': 'sync_range',
                'start': range_to_hex(leaf_end),
                'end': range_to_hex(end),
                'hash': next_hash.hex(),
            }))

        return responses

    # Case 3: We have nothing (ZERO), they have events
    if our_hash == ZERO_HASH and their_hash != ZERO_HASH:
        # Request events by sending ZERO
        responses.append(make_response({
            'type': 'sync_range',
            'start': msg['start'],
            'end': msg['end'],
            'hash': ZERO_HASH.hex(),
        }))
        return responses

    # Case 4: Both have events but hashes differ - bisect or send
    count = count_events_in_range(db, recorded_by, start, end)

    if count <= EVENTS_THRESHOLD:
        # Small enough - send events directly
        event_ids = get_events_in_range(db, recorded_by, start, end)

        if event_ids:
            _send_event_blobs(db, recorded_by, connection_id, event_ids, t_ms)

        # Send our hash for verification
        responses.append(make_response({
            'type': 'sync_range',
            'start': msg['start'],
            'end': msg['end'],
            'hash': our_hash.hex(),
        }))

    else:
        # Too many events - bisect
        mid = range_midpoint(start, end)

        left_hash = compute_range_hash(db, recorded_by, start, mid)
        right_hash = compute_range_hash(db, recorded_by, mid, end)

        responses.append(make_response({
            'type': 'sync_range',
            'start': range_to_hex(start),
            'end': range_to_hex(mid),
            'hash': left_hash.hex(),
        }))
        responses.append(make_response({
            'type': 'sync_range',
            'start': range_to_hex(mid),
            'end': range_to_hex(end),
            'hash': right_hash.hex(),
        }))

    return responses


def handle_sync_match(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int,
) -> list[dict]:
    """Handle a sync_match confirmation.

    This is an explicit ACK that the range is synced.
    No response needed.
    """
    # CC: If this has a range_id, it's a response to our request
    their_range_id = msg.get('range_id')
    if their_range_id is not None:
        _cc_on_response(recorded_by, connection_id, t_ms, range_id=their_range_id)

    log.debug(f"sync_match: range [{msg['start']}, {msg['end']}) synced")
    return []


def handle_bisect_message(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int,
) -> list[dict]:
    """Route binary bisection protocol messages."""
    msg_type = msg.get('type')

    if msg_type == 'sync_range':
        return handle_sync_range(db, recorded_by, connection_id, msg, t_ms)
    elif msg_type == 'sync_match':
        return handle_sync_match(db, recorded_by, connection_id, msg, t_ms)
    else:
        log.warning(f"Unknown bisect message type: {msg_type}")
        return []


def initiate_sync(
    db,
    recorded_by: str,
    connection_id: str,
    t_ms: int,
) -> list[dict]:
    """Initiate sync by sending root range hash.

    Returns list of messages to send.
    """
    root_hash = compute_range_hash(db, recorded_by, RANGE_MIN, RANGE_MAX)

    return [{
        'type': 'sync_range',
        'start': range_to_hex(RANGE_MIN),
        'end': range_to_hex(RANGE_MAX),
        'hash': root_hash.hex(),
    }]


# ============================================================================
# Debug/Utility functions
# ============================================================================

def decode_unified_key(unified_key: str) -> tuple[int, int]:
    """Decode a unified key into (relative_minutes, hash_16bits).

    The unified key encodes: (relative_minutes << 16) | hash_16bits
    where relative_minutes is minutes since EPOCH_MS (2025-01-01 UTC).

    Args:
        unified_key: 12 character hex string (48 bits)

    Returns:
        Tuple of (relative_minutes, hash_16bits)
    """
    key_int = int(unified_key, 16)
    relative_min = key_int >> 16
    hash_bits = key_int & 0xFFFF
    return relative_min, hash_bits


def format_unified_key_human(unified_key: str) -> str:
    """Format a unified key for human-readable display.

    Shows relative minutes from epoch and hash suffix.
    """
    relative_min, hash_bits = decode_unified_key(unified_key)
    return f"min:{relative_min} hash:{hash_bits:04x}"
