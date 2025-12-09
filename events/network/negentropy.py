"""
Negentropy-style deterministic sync protocol - UNIFIED TIME+HASH BUCKETS variant.

This variant uses a unified key space where the bucket key is:
    timestamp (ms) || event_id_hash

This creates a single unified hierarchy where:
- Events naturally cluster by time (high-order bits are timestamp)
- Within the same timestamp, events spread by hash
- The hierarchy drills down through the combined key space

This is essentially "one big hash" but the timestamps form the high-order bits,
so temporal locality is preserved while still using pure prefix-based bucketing.

Design goals:
- Deterministic: no false positives, exact set reconciliation
- Finality: clear "done" state when synced
- UI-inspectable: track state per connection for visibility
- Connection-scoped: ranges tracked by connection_id
- Temporal locality: events from similar times cluster together
- Uniform within time: events with same timestamp spread uniformly

Bucket hierarchy (by unified key prefix):
- root: all events
- prefix_2: first 2 hex chars (256 buckets)
- prefix_4: first 4 hex chars (65536 buckets)
- prefix_6 through prefix_12: timestamp precision levels
- prefix_14, prefix_16: hash precision levels (for same-timestamp events)

The unified key is: timestamp_hex (12 chars for 48 bits) + event_hash (4 chars)
Total: 16 hex chars = 64 bits

For large files (e.g., 1GB = 2.4M slices), all slices may share the same timestamp.
The hash suffix (chars 12-16) ensures these events spread across different buckets
at the finest levels (prefix_14, prefix_16).

Protocol flow:
1. On new connection: send root hash
2. Receive their hash, compare ranges
3. For mismatched ranges: drill down by unified prefix until bucket has ≤EVENTS_THRESHOLD events
4. At threshold: send actual event IDs
5. When all ranges match: sync complete for this connection
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Any
from dataclasses import dataclass
from enum import Enum

from db import create_safe_db
import crypto

log = logging.getLogger(__name__)

# Hierarchy levels - each level adds 2 hex chars (8 bits) of the unified key
# root (0) -> prefix_2 -> ... -> prefix_12 (full timestamp) -> prefix_14 -> prefix_16 (full key)
# Levels prefix_14 and prefix_16 use the hash suffix to split same-timestamp events
LEVELS = ['root', 'prefix_2', 'prefix_4', 'prefix_6', 'prefix_8', 'prefix_10', 'prefix_12', 'prefix_14', 'prefix_16']

# Hex characters per level
LEVEL_PREFIX_LEN = {
    'root': 0,
    'prefix_2': 2,
    'prefix_4': 4,
    'prefix_6': 6,
    'prefix_8': 8,
    'prefix_10': 10,
    'prefix_12': 12,  # Full timestamp precision
    'prefix_14': 14,
    'prefix_16': 16,  # Full unified key (timestamp + hash)
}

# When bucket has this many events or fewer, send event IDs instead of drilling down
EVENTS_THRESHOLD = 100


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

def compute_unified_key(event_id: str, created_at: int) -> str:
    """Compute the unified time+hash key for an event.

    The unified key is: timestamp_hex (12 chars, 48 bits) + event_hash (4 chars, 16 bits)

    This creates a key where:
    - High-order bits are timestamp (so events cluster by time when sorted)
    - Low-order bits are hash (so events with same timestamp spread uniformly)

    48 bits of timestamp gives us ~8900 years of millisecond precision from epoch.
    16 bits of hash gives 65536 sub-buckets within each timestamp.

    Args:
        event_id: The event identifier
        created_at: Event timestamp in milliseconds since epoch

    Returns:
        16 character hex string (64 bits total)
    """
    # Timestamp: 48 bits (6 bytes) - gives us ms precision for ~8900 years
    # We mask to 48 bits to ensure consistent length
    ts_masked = created_at & 0xFFFFFFFFFFFF  # 48 bits
    ts_hex = format(ts_masked, '012x')  # 12 hex chars

    # Event hash: 16 bits (2 bytes) for sub-bucket distribution
    h = hashlib.blake2b(event_id.encode('utf-8'), digest_size=2).digest()
    hash_hex = h.hex()  # 4 hex chars

    return ts_hex + hash_hex


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
# Hash computation
# ============================================================================

def compute_leaf_hash(event_ids: list[str]) -> bytes:
    """Compute hash for a leaf bucket.

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


def compute_parent_hash(child_hashes: dict[str, bytes]) -> bytes:
    """Compute hash for a parent bucket from child hashes.

    BLAKE2b-128 of sorted (prefix, hash) pairs, excluding empty children.
    """
    # Filter out empty children
    non_empty = {k: v for k, v in child_hashes.items() if v}
    if not non_empty:
        return b''  # All children empty -> parent empty

    # Sort by prefix for determinism
    items = sorted(non_empty.items())
    # Concatenate prefix (as utf-8) + hash
    data = b''.join(k.encode('utf-8') + h for k, h in items)
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
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Compute the unified key
    unified_key = compute_unified_key(event_id, created_at)

    # Insert event -> bucket mapping
    safedb.execute("""
        INSERT OR IGNORE INTO negentropy_events
        (recorded_by, event_id, unified_key, created_at)
        VALUES (?, ?, ?, ?)
    """, (recorded_by, event_id, unified_key, created_at))

    # Mark leaf bucket hash as stale (NULL)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    leaf_prefix = unified_key[:LEVEL_PREFIX_LEN['prefix_16']]
    safedb.execute("""
        INSERT INTO negentropy_buckets
        (recorded_by, level, prefix, hash, event_count, updated_at)
        VALUES (?, 'prefix_16', ?, NULL, 1, ?)
        ON CONFLICT (recorded_by, level, prefix) DO UPDATE SET
            hash = NULL,
            event_count = event_count + 1,
            updated_at = excluded.updated_at
    """, (recorded_by, leaf_prefix, now))

    # Mark all ancestor buckets as needing recompute
    for level in ['prefix_14', 'prefix_12', 'prefix_10', 'prefix_8', 'prefix_6', 'prefix_4', 'prefix_2', 'root']:
        prefix_len = LEVEL_PREFIX_LEN[level]
        ancestor_prefix = unified_key[:prefix_len]
        safedb.execute("""
            INSERT INTO negentropy_buckets
            (recorded_by, level, prefix, hash, event_count, updated_at)
            VALUES (?, ?, ?, NULL, 0, ?)
            ON CONFLICT (recorded_by, level, prefix) DO UPDATE SET
                hash = NULL,
                updated_at = excluded.updated_at
        """, (recorded_by, level, ancestor_prefix, now))


def recompute_bucket_hash(
    db,
    recorded_by: str,
    level: str,
    prefix: str
) -> bytes:
    """Recompute hash for a bucket if stale.

    For leaf buckets: hash of sorted event IDs.
    For parent buckets: hash of sorted (child_prefix, child_hash) pairs.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Check if already computed
    row = safedb.query_one("""
        SELECT hash FROM negentropy_buckets
        WHERE recorded_by = ? AND level = ? AND prefix = ?
    """, (recorded_by, level, prefix))

    if row and row['hash'] is not None:
        return row['hash']

    # Compute hash
    if level == 'prefix_16':
        # Leaf: hash of event IDs
        prefix_pattern = prefix + '%'
        events = safedb.query("""
            SELECT event_id FROM negentropy_events
            WHERE recorded_by = ? AND unified_key LIKE ?
            ORDER BY event_id
        """, (recorded_by, prefix_pattern))
        event_ids = [r['event_id'] for r in events]
        hash_val = compute_leaf_hash(event_ids)
    else:
        # Parent: hash of child hashes
        child_level = get_child_level(level)
        child_prefix_len = LEVEL_PREFIX_LEN[child_level]

        if level == 'root':
            # Root's children are all prefix_2 buckets
            children = safedb.query("""
                SELECT prefix, hash FROM negentropy_buckets
                WHERE recorded_by = ? AND level = ?
            """, (recorded_by, child_level))
        else:
            # Find children with matching prefix start
            prefix_pattern = prefix + '%'
            children = safedb.query("""
                SELECT prefix, hash FROM negentropy_buckets
                WHERE recorded_by = ? AND level = ?
                AND prefix LIKE ? AND length(prefix) = ?
            """, (recorded_by, child_level, prefix_pattern, child_prefix_len))

        # Recursively ensure children are computed
        child_hashes = {}
        for child_row in children:
            child_prefix = child_row['prefix']
            child_hash = child_row['hash']
            if child_hash is None:
                child_hash = recompute_bucket_hash(db, recorded_by, child_level, child_prefix)
            child_hashes[child_prefix] = child_hash

        hash_val = compute_parent_hash(child_hashes)

    # Store computed hash
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    safedb.execute("""
        INSERT INTO negentropy_buckets
        (recorded_by, level, prefix, hash, event_count, updated_at)
        VALUES (?, ?, ?, ?, 0, ?)
        ON CONFLICT (recorded_by, level, prefix) DO UPDATE SET
            hash = excluded.hash,
            updated_at = excluded.updated_at
    """, (recorded_by, level, prefix, hash_val, now))

    return hash_val


def get_hashes_at_level(
    db,
    recorded_by: str,
    level: str,
    parent_prefix: Optional[str] = None
) -> dict[str, bytes]:
    """Get all bucket hashes at a level, optionally filtered by parent prefix.

    Recomputes stale hashes as needed.

    Returns:
        Dict mapping prefix -> hash
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
        """, (recorded_by, level, prefix_pattern, level_prefix_len))
    else:
        # All buckets at this level
        rows = safedb.query("""
            SELECT prefix, hash FROM negentropy_buckets
            WHERE recorded_by = ? AND level = ?
        """, (recorded_by, level))

    result = {}
    for row in rows:
        prefix = row['prefix']
        hash_val = row['hash']
        if hash_val is None:
            hash_val = recompute_bucket_hash(db, recorded_by, level, prefix)
        result[prefix] = hash_val

    return result


def get_events_in_bucket(
    db,
    recorded_by: str,
    prefix: str,
    level: str = 'prefix_16'
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
        if level == 'prefix_16' or event_count <= EVENTS_THRESHOLD:
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
        WHERE connection_id = ? AND recorded_by = ?
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
    from events.network import connection as conn_module

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
        conn: Connection object (must have their_connection_id set)
        t_ms: Current timestamp

    Returns:
        Number of messages sent
    """
    from events.network import connection as conn_module

    msgs = init_sync_for_connection(db, recorded_by, conn.connection_id, t_ms)
    sent = 0
    for msg in msgs:
        # Wrap in negentropy envelope for ephemeral detection
        # Include reply_connection_id so receiver knows which connection_id to use
        envelope = {
            'type': 'negentropy',
            'connection_id': conn.connection_id,
            'reply_connection_id': conn.their_connection_id,  # Receiver's connection_id
            'data': msg,
        }
        blob = crypto.canonicalize_json(envelope)
        if conn_module.send(recorded_by, conn.connection_id, blob, t_ms, db):
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
    from events.network import connection as conn_module

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
        response_envelope = {
            'type': 'negentropy',
            'connection_id': connection_id,
            'reply_connection_id': sender_connection_id,  # Original sender's connection_id
            'data': response,
        }
        blob = crypto.canonicalize_json(response_envelope)
        if conn_module.send(recorded_by, connection_id, blob, t_ms, db):
            sent += 1
    return sent


def sync_all_connections(t_ms: int, db: Any) -> dict:
    """Sync all active connections for all local peers.

    Called by NegentropySyncJob. Iterates all local peers and their
    established connections, sending negentropy root hashes to initiate
    or continue sync.

    Optimization: Skip connections where our root hash hasn't changed since
    last successful sync. This avoids sending redundant messages when idle.

    Returns:
        Stats dict with counts
    """
    from db import create_unsafe_db
    from events.network import connection as conn_module

    unsafedb = create_unsafe_db(db)
    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    total_connections = 0
    total_messages = 0
    skipped_unchanged = 0

    for peer_row in local_peers:
        peer_id = peer_row['peer_id']

        # Get our current root hash once per peer
        our_root_hash = get_root_hash(db, peer_id)

        # Get all active connections using connection module interface
        connections = conn_module.get_connections(peer_id, t_ms, db)

        for conn in connections:
            # Only sync on established bidirectional connections
            if not conn.can_send():
                continue

            # Skip bootstrap connections without peer identity
            if not conn.peer_shared_id:
                continue

            total_connections += 1

            # Optimization: Skip if our root hasn't changed since last sync
            if conn.last_synced_root_hash and conn.last_synced_root_hash == our_root_hash:
                skipped_unchanged += 1
                continue

            sent = sync_connection(db, peer_id, conn, t_ms)
            total_messages += sent

    log.info(f"negentropy.sync_all_connections: {total_connections} connections, {total_messages} sent, {skipped_unchanged} skipped (unchanged)")
    return {'connections': total_connections, 'messages_sent': total_messages, 'skipped_unchanged': skipped_unchanged}


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


# ============================================================================
# Debug/Utility functions
# ============================================================================

def decode_unified_key(unified_key: str) -> tuple[int, str]:
    """Decode a unified key back into timestamp and hash components.

    Useful for debugging and visualization.

    Args:
        unified_key: 16 character hex string

    Returns:
        Tuple of (timestamp_ms, hash_hex)
    """
    ts_hex = unified_key[:12]
    hash_hex = unified_key[12:]
    ts_ms = int(ts_hex, 16)
    return ts_ms, hash_hex


def format_unified_key_human(unified_key: str) -> str:
    """Format a unified key for human-readable display.

    Shows the timestamp as ISO format and the hash suffix.
    """
    ts_ms, hash_hex = decode_unified_key(unified_key)
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{dt.isoformat()}:{hash_hex}"
