"""
Negentropy-style deterministic sync protocol.

Design goals:
- Deterministic: no false positives, exact set reconciliation
- Finality: clear "done" state when synced
- UI-inspectable: track state per connection for visibility
- Connection-scoped: ranges tracked by connection_id

Protocol flow:
1. On new connection: send root hash
2. Receive their hash, compare ranges
3. For mismatched ranges: drill down until bucket has ≤EVENTS_THRESHOLD events
4. At threshold: send actual event IDs
5. When all ranges match: sync complete for this connection

Buckets are identified by Unix timestamps (ms), not human-readable strings.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from calendar import monthrange
from typing import Optional, Any
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)

# Hierarchy levels from coarsest to finest
LEVELS = ['root', 'year', 'month', 'day', 'hour', 'ten_min', 'one_min']

# When bucket has this many events or fewer, send blobs instead of drilling down
# Stub mode: set very high (1000000) to always send all blobs
# Production: set to 100 for efficiency
EVENTS_THRESHOLD = 1000000  # Stub mode

# For testing: which level to stop drilling at in stub mode
# 'year' = stop at year level (send events immediately)
# 'one_min' = drill all the way down (normal behavior)
STUB_THRESHOLD = 'one_min'

# Root bucket sentinel value
ROOT_BUCKET_START = 0


class RangeStatus(Enum):
    PENDING = 'pending'          # Waiting for their response
    MATCHED = 'matched'          # Hashes match, range synced
    DIVERGED = 'diverged'        # Hashes differ, need to drill down or send
    EVENTS_SENT = 'events_sent'  # We sent events, waiting for their events
    COMPLETE = 'complete'        # Range fully reconciled


@dataclass
class RangeRequest:
    """A request to sync a specific time range."""
    range_id: str
    level: str
    bucket_start_ms: int
    hashes: dict[int, bytes]  # child_start_ms -> hash


@dataclass
class RangeResponse:
    """Response to a range request."""
    range_id: str
    level: str
    bucket_start_ms: int
    hashes: dict[int, bytes]  # Our hashes for comparison


@dataclass
class EventsMessage:
    """Events for a specific bucket."""
    range_id: str
    bucket_start_ms: int
    event_ids: list[str]


# ============================================================================
# Bucket boundary computation
# ============================================================================

def get_bucket_start_ms(ts_ms: int, level: str) -> int:
    """Get the start timestamp of the bucket containing the given timestamp.

    Args:
        ts_ms: Timestamp in milliseconds since epoch
        level: One of 'root', 'year', 'month', 'day', 'hour', 'ten_min', 'one_min'

    Returns:
        Start timestamp (ms) of the bucket containing ts_ms
    """
    if level == 'root':
        return ROOT_BUCKET_START

    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

    if level == 'year':
        start_dt = dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif level == 'month':
        start_dt = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif level == 'day':
        start_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif level == 'hour':
        start_dt = dt.replace(minute=0, second=0, microsecond=0)
    elif level == 'ten_min':
        ten_min = (dt.minute // 10) * 10
        start_dt = dt.replace(minute=ten_min, second=0, microsecond=0)
    elif level == 'one_min':
        start_dt = dt.replace(second=0, microsecond=0)
    else:
        raise ValueError(f"Unknown level: {level}")

    return int(start_dt.timestamp() * 1000)


def get_bucket_end_ms(bucket_start_ms: int, level: str) -> int:
    """Get the end timestamp (exclusive) of a bucket.

    Args:
        bucket_start_ms: Start of the bucket
        level: Bucket level

    Returns:
        End timestamp (ms), which is the start of the next bucket
    """
    if level == 'root':
        # Root spans all time - use a far future date
        return 253402300800000  # Year 9999

    dt = datetime.fromtimestamp(bucket_start_ms / 1000, tz=timezone.utc)

    if level == 'year':
        end_dt = dt.replace(year=dt.year + 1)
    elif level == 'month':
        if dt.month == 12:
            end_dt = dt.replace(year=dt.year + 1, month=1)
        else:
            end_dt = dt.replace(month=dt.month + 1)
    elif level == 'day':
        # Add one day
        end_dt = datetime.fromtimestamp((bucket_start_ms / 1000) + 86400, tz=timezone.utc)
        end_dt = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif level == 'hour':
        end_dt = datetime.fromtimestamp((bucket_start_ms / 1000) + 3600, tz=timezone.utc)
        end_dt = end_dt.replace(minute=0, second=0, microsecond=0)
    elif level == 'ten_min':
        end_dt = datetime.fromtimestamp((bucket_start_ms / 1000) + 600, tz=timezone.utc)
        end_dt = end_dt.replace(second=0, microsecond=0)
    elif level == 'one_min':
        end_dt = datetime.fromtimestamp((bucket_start_ms / 1000) + 60, tz=timezone.utc)
        end_dt = end_dt.replace(second=0, microsecond=0)
    else:
        raise ValueError(f"Unknown level: {level}")

    return int(end_dt.timestamp() * 1000)


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


def format_bucket_human(bucket_start_ms: int, level: str) -> str:
    """Format a bucket timestamp as human-readable string (for display only)."""
    if level == 'root':
        return 'root'

    dt = datetime.fromtimestamp(bucket_start_ms / 1000, tz=timezone.utc)

    if level == 'year':
        return f"{dt.year}"
    elif level == 'month':
        return f"{dt.year}-{dt.month:02d}"
    elif level == 'day':
        return f"{dt.year}-{dt.month:02d}-{dt.day:02d}"
    elif level == 'hour':
        return f"{dt.year}-{dt.month:02d}-{dt.day:02d}-{dt.hour:02d}"
    elif level == 'ten_min':
        return f"{dt.year}-{dt.month:02d}-{dt.day:02d}-{dt.hour:02d}-{dt.minute // 10}"
    elif level == 'one_min':
        return f"{dt.year}-{dt.month:02d}-{dt.day:02d}-{dt.hour:02d}-{dt.minute:02d}"
    else:
        return str(bucket_start_ms)


# ============================================================================
# Hash computation
# ============================================================================

def compute_leaf_hash(event_ids: list[str]) -> bytes:
    """Compute hash for a leaf bucket (1-minute).

    BLAKE2b-128 of sorted concatenated event IDs.
    """
    if not event_ids:
        return b''  # Empty bucket has empty hash

    # Sort for determinism
    sorted_ids = sorted(event_ids)
    # Concatenate (event IDs are hex strings, so just join)
    data = ''.join(sorted_ids).encode('utf-8')
    # BLAKE2b with 16-byte (128-bit) digest
    return hashlib.blake2b(data, digest_size=16).digest()


def compute_parent_hash(child_hashes: dict[int, bytes]) -> bytes:
    """Compute hash for a parent bucket from child hashes.

    BLAKE2b-128 of sorted (start_ms, hash) pairs, excluding empty children.
    Child keys are bucket_start_ms integers.
    """
    # Filter out empty children
    non_empty = {k: v for k, v in child_hashes.items() if v}
    if not non_empty:
        return b''  # All children empty -> parent empty

    # Sort by start_ms for determinism
    items = sorted(non_empty.items())
    # Concatenate start_ms (as 8-byte big-endian) + hash
    data = b''.join(k.to_bytes(8, 'big') + h for k, h in items)
    return hashlib.blake2b(data, digest_size=16).digest()


# ============================================================================
# Database operations
# ============================================================================

def add_event_to_sync(
    db,
    recorded_by: str,
    event_id: str,
    created_at: int
) -> None:
    """Add an event to the sync system.

    Called when a new event is created or received.
    Marks all ancestor buckets as needing hash recomputation.
    """
    bucket_start_ms = get_bucket_start_ms(created_at, 'one_min')

    # Insert event -> bucket mapping
    db.execute("""
        INSERT OR IGNORE INTO negentropy_events
        (recorded_by, event_id, bucket_start_ms, created_at)
        VALUES (?, ?, ?, ?)
    """, (recorded_by, event_id, bucket_start_ms, created_at))

    # Mark leaf bucket hash as stale (NULL)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    db.execute("""
        INSERT INTO negentropy_buckets
        (recorded_by, level, bucket_start_ms, hash, event_count, updated_at)
        VALUES (?, 'one_min', ?, NULL, 1, ?)
        ON CONFLICT (recorded_by, level, bucket_start_ms) DO UPDATE SET
            hash = NULL,
            event_count = event_count + 1,
            updated_at = excluded.updated_at
    """, (recorded_by, bucket_start_ms, now))

    # Mark all ancestor buckets as needing recompute
    for level in ['ten_min', 'hour', 'day', 'month', 'year', 'root']:
        ancestor_start = get_bucket_start_ms(created_at, level)
        db.execute("""
            INSERT INTO negentropy_buckets
            (recorded_by, level, bucket_start_ms, hash, event_count, updated_at)
            VALUES (?, ?, ?, NULL, 0, ?)
            ON CONFLICT (recorded_by, level, bucket_start_ms) DO UPDATE SET
                hash = NULL,
                updated_at = excluded.updated_at
        """, (recorded_by, level, ancestor_start, now))


def recompute_bucket_hash(
    db,
    recorded_by: str,
    level: str,
    bucket_start_ms: int
) -> bytes:
    """Recompute hash for a bucket if stale.

    For leaf buckets: hash of sorted event IDs.
    For parent buckets: hash of sorted (child_start_ms, child_hash) pairs.
    """
    # Check if already computed
    row = db.query_one("""
        SELECT hash FROM negentropy_buckets
        WHERE recorded_by = ? AND level = ? AND bucket_start_ms = ?
    """, (recorded_by, level, bucket_start_ms))

    if row and row['hash'] is not None:
        return row['hash']

    # Compute hash
    if level == 'one_min':
        # Leaf: hash of event IDs
        events = db.query("""
            SELECT event_id FROM negentropy_events
            WHERE recorded_by = ? AND bucket_start_ms = ?
            ORDER BY event_id
        """, (recorded_by, bucket_start_ms))
        event_ids = [r['event_id'] for r in events]
        hash_val = compute_leaf_hash(event_ids)
    else:
        # Parent: hash of child hashes
        child_level = get_child_level(level)
        bucket_end_ms = get_bucket_end_ms(bucket_start_ms, level)

        # Find children within this bucket's time range
        children = db.query("""
            SELECT bucket_start_ms, hash FROM negentropy_buckets
            WHERE recorded_by = ? AND level = ?
            AND bucket_start_ms >= ? AND bucket_start_ms < ?
        """, (recorded_by, child_level, bucket_start_ms, bucket_end_ms))

        # Recursively ensure children are computed
        child_hashes = {}
        for child_row in children:
            child_start = child_row['bucket_start_ms']
            child_hash = child_row['hash']
            if child_hash is None:
                child_hash = recompute_bucket_hash(db, recorded_by, child_level, child_start)
            child_hashes[child_start] = child_hash

        hash_val = compute_parent_hash(child_hashes)

    # Store computed hash
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    db.execute("""
        INSERT INTO negentropy_buckets
        (recorded_by, level, bucket_start_ms, hash, event_count, updated_at)
        VALUES (?, ?, ?, ?, 0, ?)
        ON CONFLICT (recorded_by, level, bucket_start_ms) DO UPDATE SET
            hash = excluded.hash,
            updated_at = excluded.updated_at
    """, (recorded_by, level, bucket_start_ms, hash_val, now))

    return hash_val


def get_hashes_at_level(
    db,
    recorded_by: str,
    level: str,
    parent_start_ms: Optional[int] = None,
    parent_level: Optional[str] = None
) -> dict[int, bytes]:
    """Get all bucket hashes at a level, optionally filtered by parent.

    Recomputes stale hashes as needed.

    Returns:
        Dict mapping bucket_start_ms -> hash
    """
    if parent_start_ms is not None and parent_level is not None:
        # Children within parent's time range
        parent_end_ms = get_bucket_end_ms(parent_start_ms, parent_level)
        rows = db.query("""
            SELECT bucket_start_ms, hash FROM negentropy_buckets
            WHERE recorded_by = ? AND level = ?
            AND bucket_start_ms >= ? AND bucket_start_ms < ?
        """, (recorded_by, level, parent_start_ms, parent_end_ms))
    else:
        # All buckets at this level
        rows = db.query("""
            SELECT bucket_start_ms, hash FROM negentropy_buckets
            WHERE recorded_by = ? AND level = ?
        """, (recorded_by, level))

    result = {}
    for row in rows:
        bucket_start = row['bucket_start_ms']
        hash_val = row['hash']
        if hash_val is None:
            hash_val = recompute_bucket_hash(db, recorded_by, level, bucket_start)
        result[bucket_start] = hash_val

    return result


def get_events_in_bucket(
    db,
    recorded_by: str,
    bucket_start_ms: int
) -> list[str]:
    """Get all event IDs in a 1-minute bucket."""
    rows = db.query("""
        SELECT event_id FROM negentropy_events
        WHERE recorded_by = ? AND bucket_start_ms = ?
        ORDER BY event_id
    """, (recorded_by, bucket_start_ms))
    return [r['event_id'] for r in rows]


def get_total_event_count(db, recorded_by: str) -> int:
    """Get total number of events being synced."""
    row = db.query_one("""
        SELECT COUNT(*) as cnt FROM negentropy_events
        WHERE recorded_by = ?
    """, (recorded_by,))
    return row['cnt'] if row else 0


def get_root_hash(db, recorded_by: str) -> bytes:
    """Get the root hash for this peer's event set."""
    return recompute_bucket_hash(db, recorded_by, 'root', ROOT_BUCKET_START)


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
    # Get root hash
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    if not root_hash:
        # No events, nothing to sync
        return []

    range_id = generate_range_id()

    # Record sync state
    db.execute("""
        INSERT INTO negentropy_sync_state
        (recorded_by, connection_id, range_id, level, bucket_start_ms,
         our_hash, their_hash, status, created_at, updated_at)
        VALUES (?, ?, ?, 'root', ?, ?, NULL, 'pending', ?, ?)
    """, (recorded_by, connection_id, range_id, ROOT_BUCKET_START, root_hash, t_ms, t_ms))

    return [{
        'type': 'range_request',
        'range_id': range_id,
        'level': 'root',
        'bucket_start_ms': ROOT_BUCKET_START,
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
    range_id = msg['range_id']
    level = msg['level']
    bucket_start_ms = msg['bucket_start_ms']
    their_hash = bytes.fromhex(msg['hash']) if msg['hash'] else b''

    # Get root hash and total events for all responses
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    # Compute our hash for this bucket
    our_hash = recompute_bucket_hash(db, recorded_by, level, bucket_start_ms)

    # Record the range
    db.execute("""
        INSERT INTO negentropy_sync_state
        (recorded_by, connection_id, range_id, level, bucket_start_ms,
         our_hash, their_hash, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT (recorded_by, connection_id, range_id) DO UPDATE SET
            their_hash = excluded.their_hash,
            updated_at = excluded.updated_at
    """, (recorded_by, connection_id, range_id, level, bucket_start_ms, our_hash, their_hash, t_ms, t_ms))

    # Check for checkpoint: if their root hash matches ours
    their_root_hash = bytes.fromhex(msg.get('root_hash', '')) if msg.get('root_hash') else None
    if their_root_hash and their_root_hash == root_hash:
        _log_checkpoint(db, recorded_by, connection_id, root_hash, t_ms)

    responses = []

    if our_hash == their_hash:
        # Hashes match - this range is synced!
        db.execute("""
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

    elif level == 'one_min' or LEVELS.index(level) >= LEVELS.index(STUB_THRESHOLD):
        # At leaf level or stub threshold: send events
        db.execute("""
            UPDATE negentropy_sync_state
            SET status = 'events_sent', updated_at = ?
            WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
        """, (t_ms, recorded_by, connection_id, range_id))

        event_ids = get_events_in_bucket(db, recorded_by, bucket_start_ms)
        responses.append({
            'type': 'range_events',
            'range_id': range_id,
            'bucket_start_ms': bucket_start_ms,
            'event_ids': event_ids,
            'our_hash': our_hash.hex() if our_hash else '',
            'root_hash': root_hash.hex() if root_hash else '',
            'total_events': total_events,
        })

    else:
        # Drill down: send child hashes
        child_level = get_child_level(level)
        child_hashes = get_hashes_at_level(db, recorded_by, child_level, bucket_start_ms, level)

        db.execute("""
            UPDATE negentropy_sync_state
            SET status = 'diverged', updated_at = ?
            WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
        """, (t_ms, recorded_by, connection_id, range_id))

        # Create child ranges
        for child_start, child_hash in child_hashes.items():
            child_range_id = generate_range_id()

            db.execute("""
                INSERT INTO negentropy_sync_state
                (recorded_by, connection_id, range_id, level, bucket_start_ms,
                 our_hash, their_hash, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?)
            """, (recorded_by, connection_id, child_range_id, child_level, child_start, child_hash, t_ms, t_ms))

            responses.append({
                'type': 'range_request',
                'range_id': child_range_id,
                'level': child_level,
                'bucket_start_ms': child_start,
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
    """Log a sync checkpoint when root hashes match."""
    # Get sync stats
    stats = db.query_one("""
        SELECT
            COUNT(CASE WHEN status = 'events_sent' THEN 1 END) as events_sent,
            COUNT(*) as ranges_checked
        FROM negentropy_sync_state
        WHERE recorded_by = ? AND connection_id = ?
    """, (recorded_by, connection_id))

    db.execute("""
        INSERT INTO negentropy_checkpoints
        (recorded_by, connection_id, completed_at, root_hash,
         events_sent, events_received, ranges_checked)
        VALUES (?, ?, ?, ?, ?, 0, ?)
    """, (recorded_by, connection_id, t_ms, root_hash,
          stats['events_sent'] if stats else 0,
          stats['ranges_checked'] if stats else 0))


def handle_range_matched(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int
) -> list[dict]:
    """Handle confirmation that a range matches."""
    range_id = msg['range_id']

    db.execute("""
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


def handle_range_events(
    db,
    recorded_by: str,
    connection_id: str,
    msg: dict,
    t_ms: int
) -> list[dict]:
    """Handle incoming events for a bucket.

    Returns events we have that they might not.
    """
    range_id = msg['range_id']
    bucket_start_ms = msg['bucket_start_ms']
    their_event_ids = set(msg['event_ids'])

    # Get root hash and total events for responses
    root_hash = get_root_hash(db, recorded_by)
    total_events = get_total_event_count(db, recorded_by)

    # Check for checkpoint
    their_root_hash = bytes.fromhex(msg.get('root_hash', '')) if msg.get('root_hash') else None
    if their_root_hash and their_root_hash == root_hash:
        _log_checkpoint(db, recorded_by, connection_id, root_hash, t_ms)

    # Get our events for this bucket
    our_event_ids = set(get_events_in_bucket(db, recorded_by, bucket_start_ms))

    # Events they have that we don't -> we need to request/store
    # (The actual event blobs would come through the normal event channel)
    events_we_need = their_event_ids - our_event_ids

    # Events we have that they don't -> send back
    events_they_need = our_event_ids - their_event_ids

    # Mark range complete
    db.execute("""
        UPDATE negentropy_sync_state
        SET status = 'complete', updated_at = ?
        WHERE recorded_by = ? AND connection_id = ? AND range_id = ?
    """, (t_ms, recorded_by, connection_id, range_id))

    responses = []

    if events_they_need:
        responses.append({
            'type': 'range_events',
            'range_id': range_id,
            'bucket_start_ms': bucket_start_ms,
            'event_ids': list(events_they_need),
            'final': True,  # This completes the exchange
            'root_hash': root_hash.hex() if root_hash else '',
            'total_events': total_events,
        })

    # Note: events_we_need would trigger event requests through normal channels
    # The sync protocol just identifies WHAT to sync, not the actual event content

    return responses


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

def sync(
    db,
    recorded_by: str,
    connection,  # Must have .id and .send(event)
    t_ms: int
) -> None:
    """Start sync with a peer over a connection.

    Sends root hash to initiate negotiation.
    Call this when a connection is established.

    Incoming negentropy events are handled by project().
    """
    events = init_sync_for_connection(db, recorded_by, connection.id, t_ms)
    for event in events:
        connection.send(event)


def project(
    db,
    recorded_by: str,
    event: dict,
    t_ms: int,
    get_connection,  # callable: connection_id -> connection
) -> None:
    """Project an incoming negentropy event.

    Processes the event, updates sync state, and sends responses
    on the connection the event arrived on.
    """
    connection_id = event['connection_id']
    connection = get_connection(connection_id)
    responses = handle_sync_message(db, recorded_by, connection_id, event, t_ms)
    for response in responses:
        connection.send(response)


def get_sync_status(
    db,
    recorded_by: str,
    connection_id: str
) -> dict:
    """Get sync status for UI display.

    Returns counts of ranges in each state.
    """
    rows = db.query("""
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
