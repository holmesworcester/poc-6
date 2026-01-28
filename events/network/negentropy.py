"""
Negentropy-style deterministic sync protocol - HASH-ONLY BUCKETS variant.

This variant uses pure hash-based bucketing - the unified key is derived
entirely from the event_id (hash bytes), with no timestamp dependency. This ensures
that all peers compute identical keys for the same events, enabling
accurate root hash comparison for sync detection.

Design goals:
- Deterministic: no false positives, exact set reconciliation
- Finality: clear "done" state when synced (root hashes match)
- UI-inspectable: track state per connection for visibility
- Connection-scoped: ranges tracked by connection_id
- Peer-consistent: same event_id always produces same unified_key

Bucket hierarchy (by hash prefix):
- root: all events
- prefix_2: first 2 hex chars (256 buckets)
- prefix_4: first 4 hex chars (65536 buckets)
- prefix_6 through prefix_6: progressively finer hash buckets

The unified key is: event_id_bytes[:8] as 16 hex chars
This provides uniform distribution across all bucket levels.

Trade-offs vs time-based bucketing:
- Loses temporal locality (can't efficiently sync "just recent events")
- Gains peer consistency (same event = same bucket on all peers)
- Better for encrypted events where created_at is unavailable

Protocol flow:
1. On new connection: send root hash
2. Receive their hash, compare ranges
3. For mismatched ranges: drill down by hash prefix until bucket has ≤EVENTS_THRESHOLD events
4. At threshold: send actual event IDs
5. When root hashes match: sync complete for this connection
"""

import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Any
from dataclasses import dataclass
from enum import Enum

from core.db import create_safe_db
from core import crypto
from core import wire_format
from core import store
from core.projection_v2.types import ProjectorResult, Command
from core.projection_v2.apply import register_command_handler
from events.network import sync_window

log = logging.getLogger(__name__)


# Registry metadata
EVENT_TYPE = 'negentropy'
SHAREABLE = False  # Point-to-point sync protocol, don't broadcast to others
PROJECTION_TABLE = None  # No persistent projection table

# v2 event specification - minimal, as this is a sync protocol message
EVENT_SPEC = {
    'encrypted': False,  # Plain JSON sync protocol message
    'signer': None,  # No signature verification
    'requires': {},  # No dependencies
    'optional': {},
    'cascade_on_delete': [],
}


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


# Hierarchy levels - reduced to 4 levels for efficiency
# root (0) -> prefix_2 -> prefix_4 -> prefix_6
# prefix_6 (24 bits) = 16.7M possible buckets, enough for 100GB+ files
# With EVENTS_THRESHOLD=100, this handles up to 1.67 billion events
LEVELS = ['root', 'prefix_2', 'prefix_4', 'prefix_6']

# Hex characters per level
LEVEL_PREFIX_LEN = {
    'root': 0,
    'prefix_2': 2,   # 256 buckets
    'prefix_4': 4,   # 65,536 buckets
    'prefix_6': 6,   # 16,777,216 buckets
}

# When bucket has this many events or fewer, send event IDs instead of drilling down
# 100 is a good balance: small enough for reliable delivery, large enough for efficiency
EVENTS_THRESHOLD = 50


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
    """Compute the unified hash key for an event.

    The unified key is derived from the event_id hash bytes.
    This avoids re-hashing event_ids and remains peer-consistent.

    Args:
        event_id: The event identifier
        created_at: Ignored (kept for API compatibility during transition)

    Returns:
        16 character hex string (64 bits of hash)
    """
    # Use the event_id hash bytes directly when possible
    try:
        raw = crypto.b64decode(event_id)
        if len(raw) >= 8:
            return raw[:8].hex()
    except Exception:
        pass

    # Fallback for non-standard test IDs
    h = hashlib.blake2b(event_id.encode('utf-8'), digest_size=8).digest()
    return h.hex()  # 16 hex chars


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
        max_events = wire_format.NEGENTROPY_EVENT_ID_MAX
        if level == 'prefix_6' or event_count <= max_events:
            safedb.execute("""
                UPDATE negentropy_sync_state
                SET status = 'events_sent', updated_at = ?
                WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
            """, (t_ms, recorded_by, connection_id, range_id))

            event_ids = get_events_in_bucket(db, recorded_by, prefix, level)
            if len(event_ids) > max_events:
                log.warning(
                    f"negentropy: truncating {len(event_ids)} event_ids to {max_events} for wire payload"
                )
                event_ids = event_ids[:max_events]

            # Send actual event blobs - they'll dedupe on their side
            if event_ids:
                sent = _send_event_blobs(db, recorded_by, connection_id, event_ids, t_ms)
                log.info(f"negentropy: sent {sent} event blobs at {level} level ({event_count} events in bucket)")

            # Also send the event IDs so they know what we have (for bidirectional sync)
            responses.append({
                'type': 'range_events',
                'range_id': range_id,
                'prefix': prefix,
                'event_ids': event_ids,
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
        blob = wire_format.encode_negentropy_wire_event(
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
        blob = wire_format.encode_negentropy_wire_event(
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
