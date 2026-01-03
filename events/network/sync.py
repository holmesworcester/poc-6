"""Sync implementation with bloom-based window protocol.

Uses negentropy for efficient set reconciliation between peers.
Bloom filter sync is used as a fallback for window-based partitioning.
"""

# Registry metadata
EVENT_TYPE = 'sync'
SHAREABLE = True  # Sync events exchange between peers
EPHEMERAL = False
PROJECTION_TABLE = None

from typing import Any, Iterator
from events.network import recorded, transit_prekey, sync_window
from events.identity import peer
from db import create_safe_db, create_unsafe_db
from events.network import connection as conn_module
import queues
import crypto
import store
import hashlib
import struct
import logging

log = logging.getLogger(__name__)

# Bloom filter parameters
BLOOM_SIZE_BITS = 512  # 512 bits = 64 bytes
BLOOM_SIZE_BYTES = 64
K_HASHES = 5  # Number of hash functions

# Event types that are sync protocol infrastructure (not stored in event log)
EPHEMERAL_EVENT_TYPES = {'sync', 'sync_file', 'connection', 'negentropy'}

# Window parameters
DEFAULT_W = 12  # Default window parameter: 2^12 = 4096 windows
STORAGE_W = 20  # Storage window parameter: 2^20 = 1M windows (future-proof)
EVENTS_PER_WINDOW_TARGET = 450  # Target events per window for optimal FPR

# False Positive Rate target (informational)
TARGET_FPR = 0.025  # ~2.5% wasted bandwidth from false positives


# ============================================================================
# Bloom Filter Functions
# ============================================================================

def create_bloom(event_ids: list[bytes], salt: bytes) -> bytes:
    """Create bloom filter from list of event IDs requester HAS."""
    bloom = bytearray(BLOOM_SIZE_BYTES)
    for event_id in event_ids:
        for k in range(K_HASHES):
            bit_index = _hash_to_bit_index(event_id, salt, k)
            byte_index = bit_index // 8
            bit_offset = bit_index % 8
            bloom[byte_index] |= (1 << bit_offset)
    return bytes(bloom)


def check_bloom(event_id: bytes, bloom: bytes, salt: bytes) -> bool:
    """Check if event ID is in bloom (True=probably in, False=definitely not in)."""
    for k in range(K_HASHES):
        bit_index = _hash_to_bit_index(event_id, salt, k)
        byte_index = bit_index // 8
        bit_offset = bit_index % 8
        if not (bloom[byte_index] & (1 << bit_offset)):
            return False
    return True


def _hash_to_bit_index(event_id: bytes, salt: bytes, k: int) -> int:
    """Hash event_id with salt and k to get bit index in [0, 512)."""
    h = hashlib.blake2b(
        event_id + salt,
        digest_size=8,
        person=f"bloom-k{k}".encode()[:16]
    )
    hash_val = int.from_bytes(h.digest(), byteorder='little')
    return hash_val % BLOOM_SIZE_BITS


# ============================================================================
# Window Functions
# ============================================================================

def compute_window_id(event_id: bytes, w: int) -> int:
    """Compute window ID: high-order w bits of BLAKE2b-256(event_id)."""
    h = hashlib.blake2b(event_id, digest_size=32)
    hash_int = int.from_bytes(h.digest(), byteorder='big')
    return hash_int >> (256 - w)


def compute_storage_window_id(event_id_bytes: bytes) -> int:
    """Compute window ID for storage at w=20 to support large event counts."""
    return compute_window_id(event_id_bytes, STORAGE_W)


def derive_salt(peer_pk: bytes, window_id: int) -> bytes:
    """Derive 16-byte bloom salt: BLAKE2b-128(peer_pk || window_id)."""
    window_id_bytes = window_id.to_bytes(4, byteorder='big')
    h = hashlib.blake2b(peer_pk + window_id_bytes, digest_size=16)
    return h.digest()


def compute_w_for_event_count(total_events: int) -> int:
    """Compute optimal w for event count (target ~450 events/window)."""
    if total_events == 0:
        return DEFAULT_W
    import math
    target_windows = max(1, total_events // EVENTS_PER_WINDOW_TARGET)
    return max(1, math.ceil(math.log2(target_windows)))


def compute_window_count(w: int) -> int:
    """Compute total number of windows: 2^w."""
    return 2 ** w


def walk_windows(w: int, last_window: int = -1, peer_pk: bytes = b'') -> Iterator[int]:
    """Generate window IDs to sync in order, starting after last_window."""
    total_windows = 2 ** w
    start = (last_window + 1) % total_windows
    for i in range(total_windows):
        yield (start + i) % total_windows


# ============================================================================
# Sync State Functions
# ============================================================================

def get_sync_state(from_peer_id: str, to_peer_id: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Get sync state for peer pair (last_window, w_param, total_events_seen)."""
    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one(
        "SELECT last_window, w_param, total_events_seen FROM sync_state_ephemeral WHERE from_peer_id = ? AND to_peer_id = ?",
        (from_peer_id, to_peer_id)
    )
    if row:
        return {
            'last_window': row['last_window'],
            'w_param': row['w_param'],
            'total_events_seen': row['total_events_seen']
        }

    # Start with w=0 (1 window covering entire event space) for initial sync
    # This ensures ALL events are included in the first sync request bloom filter
    # w_param will auto-adjust upward as events are synced
    return {
        'last_window': -1,
        'w_param': 0,
        'total_events_seen': 0
    }


def update_sync_state(
    from_peer_id: str,
    to_peer_id: str,
    last_window: int,
    w_param: int,
    total_events_seen: int,
    t_ms: int,
    db: Any
) -> None:
    """Update sync state for peer pair."""
    unsafedb = create_unsafe_db(db)
    unsafedb.execute(
        """INSERT INTO sync_state_ephemeral (from_peer_id, to_peer_id, last_window, w_param, total_events_seen, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (from_peer_id, to_peer_id)
           DO UPDATE SET
               last_window = excluded.last_window,
               w_param = excluded.w_param,
               total_events_seen = excluded.total_events_seen,
               updated_at = excluded.updated_at""",
        (from_peer_id, to_peer_id, last_window, w_param, total_events_seen, t_ms)
    )


def get_next_window(from_peer_id: str, to_peer_id: str, t_ms: int, db: Any) -> tuple[int, int]:
    """Get next window to sync for peer pair (window_id, w_param)."""
    state = get_sync_state(from_peer_id, to_peer_id, t_ms, db)
    # Use SyncWindow to compute next window
    window = sync_window.SyncWindow(w=state['w_param'], query_window_id=state['last_window'])
    next_window = window.next_window(state['last_window'])
    return next_window, state['w_param']


def mark_window_synced(from_peer_id: str, to_peer_id: str, window_id: int, t_ms: int, db: Any) -> None:
    """Mark window as synced and adjust w_param based on requester's total event count."""
    log.debug(f"mark_window_synced: from={from_peer_id[:20]}... to={to_peer_id[:20]}... window={window_id}")
    state = get_sync_state(from_peer_id, to_peer_id, t_ms, db)
    state['last_window'] = window_id

    # Count total shareable events for requester (events they've seen and can share)
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    total_events_row = safedb.query_one(
        "SELECT COUNT(*) as count FROM shareable_events WHERE can_share_peer_id = ?",
        (from_peer_id,)
    )
    total_events = total_events_row['count'] if total_events_row else 0

    # Compute optimal w_param for this event count using SyncWindow
    optimal_w = sync_window.SyncWindow.optimal_w_for_event_count(total_events)
    state['w_param'] = max(state['w_param'], optimal_w)
    state['total_events_seen'] = total_events

    update_sync_state(
        from_peer_id,
        to_peer_id,
        state['last_window'],
        state['w_param'],
        state['total_events_seen'],
        t_ms,
        db
    )


# ============================================================================
# Connection Management Functions (delegate to connection module)
# ============================================================================


def remove_connections_for_peer(peer_shared_id: str, recorded_by: str, db: Any) -> int:
    """Remove all connections to a specific peer.

    Delegated to connection module which manages the connections table.

    Args:
        peer_shared_id: The peer_shared_id to remove connections for
        recorded_by: The local peer's perspective (unused - connection module iterates all)
        db: Database connection

    Returns:
        Number of connections deleted
    """
    from events.network import connection
    return connection.remove_connections_for_peer(peer_shared_id, db)


def remove_connections_for_user(user_id: str, recorded_by: str, db: Any) -> int:
    """Remove all connections to all peers belonging to a user.

    Delegated to connection module which manages the connections table.

    Args:
        user_id: The user_id whose peers should have connections removed
        recorded_by: The local peer's perspective (unused - connection module iterates all)
        db: Database connection

    Returns:
        Total number of connections deleted
    """
    from events.network import connection
    return connection.remove_connections_for_user(user_id, db)


# ============================================================================
# Core Sync Functions
# ============================================================================


def add_shareable_events_batch(
    events: list[tuple[str, int, int]],  # List of (event_id, created_at, recorded_at)
    can_share_peer_id: str,
    db: Any,
    skip_negentropy: bool = False
) -> None:
    """Add multiple shareable events efficiently.

    Batches inserts for both shareable_events and negentropy tables.
    Much faster than calling add_shareable_event() in a loop for bulk operations.

    Args:
        events: List of (event_id, created_at, recorded_at) tuples
        can_share_peer_id: The peer who recorded/has these events
        db: Database connection
        skip_negentropy: If True, skip negentropy bucket updates (caller will batch them)
    """
    if not events:
        return

    from events.network import negentropy

    safedb = create_safe_db(db, recorded_by=can_share_peer_id)

    # Batch insert into shareable_events
    for event_id, created_at, recorded_at in events:
        event_id_bytes = crypto.b64decode(event_id)
        window_id = sync_window.SyncWindow.storage_window_from_event_id(event_id_bytes)

        safedb.execute(
            """INSERT OR IGNORE INTO shareable_events (event_id, can_share_peer_id, created_at, recorded_at, window_id)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, can_share_peer_id, created_at, recorded_at, window_id)
        )

    # Batch add to negentropy using the new batch function (unless skipped)
    if not skip_negentropy:
        negentropy_events = [(event_id, recorded_at) for event_id, created_at, recorded_at in events]
        negentropy.add_events_to_sync_batch(db, can_share_peer_id, negentropy_events)

    log.debug(f"add_shareable_events_batch: added {len(events)} events for peer={can_share_peer_id[:20]}... (skip_neg={skip_negentropy})")


def add_shareable_event(event_id: str, can_share_peer_id: str, created_at: int, recorded_at: int, db: Any,
                        skip_negentropy: bool = False) -> None:
    """Add shareable event to both sync tracking tables.

    ARCHITECTURE NOTE: Dual Sync Tables
    ====================================
    We maintain two tables for sync tracking:

    1. shareable_events - Used by bloom filter sync protocol
       - Tracks window_id for hash-based windowing
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


def route_blob_to_peers(blob: bytes, db: Any) -> list[str]:
    """Device-wide routing: determine which local peers can decrypt this blob.

    Checks connections (symmetric), transit keys (symmetric), and transit prekeys
    (asymmetric) to find all local peers who have the decryption key for this blob.

    Args:
        blob: Transit-wrapped blob with hint in first 16 bytes
        db: Database connection

    Returns:
        List of peer_ids who can decrypt this blob (empty if no keys found)
    """

    hint = blob[:16]
    hint_b64 = crypto.b64encode(hint)

    # Try connections first (symmetric keys from connection handshake)
    # The hint is first 16 bytes of connection_id hash
    try:
        cursor = db._conn.execute(
            "SELECT DISTINCT connection_id, recorded_by FROM connections"
        )
        recorded_by_peers = []
        for row in cursor.fetchall():
            conn_id = row[0]
            try:
                conn_id_bytes = crypto.b64decode(conn_id)
                if conn_id_bytes[:16] == hint:
                    recorded_by_peers.append(row[1])
            except Exception:
                continue
        if recorded_by_peers:
            log.debug(f"route_blob_to_peers: routed to {len(recorded_by_peers)} peers via connection")
            return recorded_by_peers
    except Exception as e:
        log.warning(f"route_blob_to_peers: Failed to query connections: {e}")

    # Try transit prekeys (asymmetric) - look up OWNER, not who knows about it
    try:
        cursor = db._conn.execute(
            "SELECT DISTINCT owner_peer_id FROM transit_prekeys WHERE transit_prekey_id = ?",
            (hint_b64,)
        )
        recorded_by_peers = [row[0] for row in cursor.fetchall()]
        if recorded_by_peers:
            log.debug(f"route_blob_to_peers: routed to {len(recorded_by_peers)} peers via transit_prekey")
    except Exception as e:
        log.warning(f"route_blob_to_peers: Failed to query transit_prekeys: {e}")
        recorded_by_peers = []

    return recorded_by_peers


def _project_ephemeral_for_peer(event_id: str, event_type: str, event_data: dict, recorded_by: str, t_ms: int, db: Any) -> None:
    """Project ephemeral event for a single peer.

    Handles type-specific projection and marks event as valid.
    """

    # Type-specific projection dispatch
    if event_type == 'sync':
        project(event_id, recorded_by, t_ms, db, sync_data=event_data)
    elif event_type == 'sync_file':
        from events.network import sync_file
        sync_file.project(event_id, recorded_by, t_ms, db, sync_file_data=event_data)
    elif event_type == 'connection':
        from events.network import connection
        connection.project(event_id, recorded_by, t_ms, db)
    elif event_type == 'negentropy':
        from events.network import negentropy
        # The envelope contains:
        # - connection_id: sender's connection_id (for our reply_connection_id)
        # - reply_connection_id: OUR connection_id to use for responses
        our_connection_id = event_data.get('reply_connection_id')
        if our_connection_id:
            negentropy.handle_incoming(db, recorded_by, our_connection_id, event_data, t_ms)
        else:
            log.warning(f"negentropy: no reply_connection_id in envelope")

    # Mark ephemeral event as valid (for sync protocol tracking)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (event_id, recorded_by)
    )


def handle_ephemeral_event(unwrapped_blob: bytes, event_data: dict, recorded_by_peers: list[str], t_ms: int, db: Any) -> bool:
    """Check if event is ephemeral and handle it without storing.

    Ephemeral events are protocol infrastructure that bypass normal storage
    and are projected directly for all peers who can access them.

    Args:
        unwrapped_blob: Decrypted event blob
        event_data: Parsed event data
        recorded_by_peers: List of peers who can decrypt this event
        t_ms: Current timestamp
        db: Database connection

    Returns:
        True if event was ephemeral and handled, False to proceed with normal storage
    """
    logger = log  # Alias to avoid scoping issues with loop variable

    event_type = event_data.get('type')
    if event_type not in EPHEMERAL_EVENT_TYPES:
        return False

    # Ephemeral event: project directly without storing
    logger.debug(f"handle_ephemeral_event: processing ephemeral {event_type} for {len(recorded_by_peers)} peers")
    event_id = crypto.b64encode(crypto.hash(unwrapped_blob))

    # Peers now sync normally via established connections (sync_connect)
    # No need for bootstrap_complete tracking

    # Project event for each peer who can access it
    for recorded_by in recorded_by_peers:
        _project_ephemeral_for_peer(event_id, event_type, event_data, recorded_by, t_ms, db)

    return True


def unwrap_and_store(blob: bytes, t_ms: int, db: Any) -> list[str]:
    """Unwrap transit blob, store event, create recorded events for all peers with access.

    Edge case: If multiple local peers have the same key (e.g., two peers in the same network
    both accepted the same invite), this creates a separate recorded event for each peer.

    Returns:
        List of recorded_ids (one per peer who can decrypt), or empty list if unwrap fails
    """

    # Route to peers who can decrypt (device-wide lookup)
    recorded_by_peers = route_blob_to_peers(blob, db)

    if not recorded_by_peers:
        log.debug(f"unwrap_and_store: no peers can decrypt blob (unknown key)")
        return []

    # Try to unwrap with each peer who has access (for prekeys, only owner can decrypt)
    unwrapped_blob = None
    for peer_id in recorded_by_peers:
        unwrapped_blob, missing_keys = crypto.unwrap_transit(blob, peer_id, db)
        if unwrapped_blob is not None:
            break

    if unwrapped_blob is None:
        log.debug(f"unwrap_and_store: unwrap failed for {len(recorded_by_peers)} peers")
        return []

    # Check if this is an ephemeral protocol event (sync events, etc.)
    try:
        event_data = crypto.parse_json(unwrapped_blob)
        if handle_ephemeral_event(unwrapped_blob, event_data, recorded_by_peers, t_ms, db):
            return []  # Ephemeral event was handled, no recorded events created
    except Exception as e:
        # Not JSON or parse failed, continue with normal storage
        pass

    # Store the unwrapped event blob (once)
    event_id = store.blob(unwrapped_blob, t_ms, True, db)
    log.debug(f"unwrap_and_store: stored event {event_id[:20]}..., creating recorded for {len(recorded_by_peers)} peers")

    # Create recorded event for EACH peer who can decrypt
    recorded_ids = []
    for recorded_by in recorded_by_peers:
        recorded_id = recorded.create(event_id, recorded_by, t_ms, db, True)
        recorded_ids.append(recorded_id)

    return recorded_ids


def _process_address_observations(transit_blobs: list[bytes], t_ms: int, db: Any) -> None:
    """Try to observe source peers from transit blobs (NAT integration).

    This is an optional integration point - if the network layer is initialized,
    we can use it to create address events. Otherwise, this is a no-op.
    """
    try:
        from simulator import network
        from events.network import observed_address as address_module

        engine = network.get_engine()
        if not engine or not engine.peer_endpoints:
            # Network engine not initialized, skip observations
            return

        # For now, we don't have origin_ip/port in the blob metadata,
        # so we can't truly observe endpoints yet.
        # This is a placeholder for future integration.
        log.debug(f"_process_address_observations: network layer available, {len(transit_blobs)} blobs")

    except ImportError:
        # Network layer not available
        pass
    except Exception as e:
        log.debug(f"_process_address_observations: error: {e}")


def receive(batch_size: int, t_ms: int, db: Any) -> None:
    """Receive and process a batch of incoming transit blobs."""
    from events.network import negentropy

    transit_blobs = queues.incoming.drain(batch_size, t_ms, db)
    log.info(f"sync.receive: processing {len(transit_blobs)} blobs")

    # unwrap_and_store returns list of recorded_ids (one per peer who can decrypt)
    new_recorded_id_lists = []
    for blob in transit_blobs:
        result = unwrap_and_store(blob, t_ms, db)
        new_recorded_id_lists.append(result)

    # Flatten and project all recorded events
    # Skip negentropy updates during projection - we'll batch them after
    valid_recorded_ids = [id for id_list in new_recorded_id_lists for id in id_list]
    log.debug(f"sync.receive: projecting {len(valid_recorded_ids)} recorded events (skip_negentropy=True)")

    recorded.project_ids(valid_recorded_ids, db, skip_negentropy=True)

    # Batch-add all received events to negentropy (grouped by peer)
    # Query shareable_events for events just added (recorded_at = t_ms)
    # Use raw connection to bypass scoping (we need to query across all peers)
    cursor = db._conn.execute(
        "SELECT event_id, can_share_peer_id, recorded_at FROM shareable_events WHERE recorded_at = ?",
        (t_ms,)
    )
    new_shareable = [{'event_id': r[0], 'can_share_peer_id': r[1], 'recorded_at': r[2]} for r in cursor.fetchall()]

    if new_shareable:
        # Group by peer_id for batch processing
        by_peer: dict[str, list[tuple[str, int]]] = {}
        for row in new_shareable:
            peer_id = row['can_share_peer_id']
            if peer_id not in by_peer:
                by_peer[peer_id] = []
            by_peer[peer_id].append((row['event_id'], row['recorded_at']))

        # Batch-add to negentropy for each peer
        for peer_id, events in by_peer.items():
            negentropy.add_events_to_sync_batch(db, peer_id, events)
            log.debug(f"sync.receive: batch-added {len(events)} events to negentropy for peer {peer_id[:20]}...")

    # Try to integrate with network layer for address observations (optional)
    try:
        _process_address_observations(transit_blobs, t_ms, db)
    except Exception as e:
        log.debug(f"sync.receive: address observation integration not fully ready: {e}")

    db.commit()


def send_request_to_all(t_ms: int, db: Any) -> None:
    """All local peers send sync requests to all peers they've seen."""

    # Query all local peers
    unsafedb = create_unsafe_db(db)
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")
    log.debug(f"sync_all: processing {len(local_peer_rows)} local peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']

        # peer_id from DB might be bytes or base64 string - standardize to base64 string for logging
        if isinstance(peer_id, bytes):
            peer_id_str = crypto.b64encode(peer_id)
        else:
            peer_id_str = peer_id

        # Find this peer's peer_shared_id
        # Method 1: Check peers_shared table (works for validated peers)
        peer_shared_id = None
        safedb = create_safe_db(db, recorded_by=peer_id)
        candidate_rows = safedb.query(
            "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
            (peer_id,)
        )
        for row in candidate_rows:
            ps_id = row['peer_shared_id']
            try:
                ps_blob = store.get(ps_id, db)
                if not ps_blob:
                    continue
                ps_data = crypto.parse_json(ps_blob)
                if ps_data.get('type') == 'peer_shared' and ps_data.get('peer_id') == peer_id:
                    peer_shared_id = ps_id
                    break
            except Exception:
                continue

        # Method 2: Fall back to shareable_events for joiners whose peer_shared isn't validated yet
        # This handles the bootstrap case where a joiner needs to sync to receive the invite chain
        if not peer_shared_id:
            shareable_rows = safedb.query(
                "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
                (peer_id,)
            )
            for row in shareable_rows:
                event_id = row['event_id']
                try:
                    event_blob = store.get(event_id, db)
                    if not event_blob:
                        continue
                    event_data = crypto.parse_json(event_blob)
                    if event_data.get('type') == 'peer_shared' and event_data.get('peer_id') == peer_id:
                        peer_shared_id = event_id
                        log.info(f"send_request_to_all: found peer_shared_id via shareable_events fallback for {peer_id_str[:10]}...")
                        break
                except Exception:
                    continue

        if not peer_shared_id:
            log.debug(f"send_request_to_all: skipping peer {peer_id_str[:10]}... - no peer_shared_id found")
            continue

        # Send sync requests from this peer to all peers they've seen
        send_requests(peer_id, peer_shared_id, t_ms, db)

        # Send file sync requests for wanted files
        send_file_sync_requests(peer_id, t_ms, db)


def send_file_sync_requests(peer_id: str, t_ms: int, db: Any) -> None:
    """Send sync_file requests for files this peer wants to actively sync.

    Args:
        peer_id: Local peer
        t_ms: Current timestamp
        db: Database connection
    """
    from events.network import sync_file

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get all wanted files (that haven't expired)
    # Note: SafeDB requires recorded_by filter for subjective tables, which is already scoped
    wanted_files = safedb.query(
        "SELECT file_id, priority FROM file_sync_wanted WHERE recorded_by = ? AND peer_id = ? AND (ttl_ms = 0 OR ttl_ms > ?) "
        "ORDER BY priority DESC, requested_at ASC",
        (peer_id, peer_id, t_ms)
    )

    log.debug(f"send_file_sync_requests: peer={peer_id[:20]}... has {len(wanted_files)} files to sync")

    for file_row in wanted_files:
        file_id = file_row['file_id']

        # Skip if file is already complete
        if sync_file.is_file_complete(file_id, peer_id, db):
            log.debug(f"send_file_sync_requests: file_id={file_id[:20]}... already complete, skipping")
            sync_file.cancel_file_sync(file_id, peer_id, db)
            continue

        # Send file sync request to all peers
        peers_to_request = safedb.query(
            "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
            (peer_id,)
        )

        log.debug(f"send_file_sync_requests: sending file sync requests for {file_id[:20]}... to {len(peers_to_request)} peers")

        for peer_row in peers_to_request:
            to_peer = peer_row['peer_shared_id']
            try:
                sync_file.send_request(file_id, to_peer, peer_id, t_ms, db)
            except Exception as e:
                log.warning(f"send_file_sync_requests: failed to send request for {file_id[:20]}...: {e}")


def send_requests(from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send all shareable events to all active connections.

    STUB IMPLEMENTATION: Sends ALL shareable events on each sync tick.
    Future: Replace with negentropy range-based set reconciliation.

    Uses the connection module for peer-scoped connection lookups.
    This follows the two-layer architecture: connections are established first,
    then sync operates on those established connections.
    """
    # Standardize encoding for logging
    if isinstance(from_peer_id, bytes):
        peer_id_str = crypto.b64encode(from_peer_id)
    else:
        peer_id_str = from_peer_id

    # Query active connections (peer-scoped via connection module)
    connections = conn_module.get_connections(from_peer_id, t_ms, db)

    connection_labels = [conn.label[:10] + '...' for conn in connections]
    log.warning(f"[SYNC_SEND] from_peer={peer_id_str[:10]}... connections={len(connections)} ids={connection_labels}")

    unsafedb = create_unsafe_db(db)

    for conn in connections:
        # Skip connections without peer_shared_id (bootstrap-only connections)
        if not conn.peer_shared_id:
            log.debug(f"[SYNC_SEND] skipping bootstrap connection (invite_id={conn.invite_id[:10] if conn.invite_id else 'None'}...)")
            continue

        peer_shared_id = conn.peer_shared_id

        # Check if this connection is to a removed peer
        removed_check = unsafedb.query_one(
            "SELECT 1 FROM removed_peers WHERE peer_shared_id = ? LIMIT 1",
            (peer_shared_id,)
        )
        if removed_check:
            log.info(f"[SYNC_SKIP_REMOVED] skipping sync to removed peer {peer_shared_id[:20]}...")
            continue

        # Skip if we can't send to this connection (no their_key)
        if not conn.can_send():
            log.debug(f"[SYNC_SEND] skipping connection without their_key (peer={peer_shared_id[:20]}...)")
            continue

        # Send sync request on this connection
        log.warning(f"[SYNC_REQUEST] from={peer_id_str[:10]}... to_peer={peer_shared_id[:10]}...")
        send_sync_request_on_connection(conn, from_peer_id, from_peer_shared_id, t_ms, db)

    db.commit()


def send_sync_request_on_connection(conn: conn_module.Connection, from_peer_id: str,
                                     from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send sync request event on a connection.

    STUB IMPLEMENTATION: Request triggers response that sends ALL events.
    Future: Replace with negentropy range-based set reconciliation.

    Args:
        conn: Connection object with active bidirectional keys
        from_peer_id: Local peer ID sending request
        from_peer_shared_id: Local peer's public identity
        t_ms: Current timestamp
        db: Database connection
    """
    # Build sync request event
    request_data = {
        'type': 'sync',
        'signed_by': from_peer_shared_id,
        'peer_id': from_peer_id,
        'from_connection_id': conn.connection_id,  # So responder knows where to send back
        'created_at': t_ms
    }

    # Sign the request
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
    signed_request = crypto.sign_event(request_data, private_key)
    request_blob = crypto.canonicalize_json(signed_request)

    # Store as event (for projection/tracking)
    unsafedb = create_unsafe_db(db)
    event_id = store.blob(request_blob, t_ms, return_dupes=True, unsafedb=unsafedb)

    # Send via connection
    if conn_module.send(from_peer_id, conn.connection_id, request_blob, t_ms, db):
        log.info(f"[SYNC_REQUEST] sent {event_id[:20]}... on connection {conn.connection_id[:20]}...")
    else:
        log.warning(f"[SYNC_REQUEST] failed to send on connection {conn.connection_id[:20]}...")


def send_request_to_connection(their_transit_key_id: str, their_transit_key: bytes,
                               from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send bloom-based sync request to an established connection.

    The connection is identified by the recipient's transit key.
    Uses random windows instead of tracking state per-connection.

    TODO: Implement proper window state tracking per-connection for efficiency.
    Currently picks random windows which works but is inefficient.
    """
    import random

    log.debug(f"[SEND_REQUEST_TO_CONNECTION] from={from_peer_id[:20]}... to_key={their_transit_key_id[:20]}...")

    # TODO: Track window state per-connection. For now, pick random window.
    # Use w_param=4 which gives 2^4=16 query windows - faster convergence for small event counts
    # (with w=10/1024 windows, random selection is too inefficient for tests with ~20 events)
    w_param = 4
    num_query_windows = 2 ** w_param  # 16 query windows
    window_id = random.randint(0, num_query_windows - 1)

    # Use SyncWindow to compute storage window range
    window = sync_window.SyncWindow(w=w_param, query_window_id=window_id)
    window_min, window_max = window.get_storage_window_range()

    # Query events we can share in this window
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    my_events_in_window = safedb.query(
        """SELECT event_id, window_id FROM shareable_events
           WHERE can_share_peer_id = ?
             AND window_id >= ?
             AND window_id < ?
           ORDER BY recorded_at ASC""",
        (from_peer_id, window_min, window_max)
    )

    event_id_bytes_list = [crypto.b64decode(row['event_id']) for row in my_events_in_window]

    # Derive salt for bloom filter (using our own public key)
    # Use peer.get_public_key() for own key - peer_shared may not be validated yet (bootstrap)
    requester_public_key = peer.get_public_key(from_peer_id, from_peer_id, db)
    salt = derive_salt(requester_public_key, window_id)
    bloom_filter = create_bloom(event_id_bytes_list, salt)

    # Create transit key for response (inline - no separate transit_key table)
    # Generate a fresh symmetric key for the response
    response_transit_key_bytes = crypto.generate_secret()
    response_transit_key_id = crypto.b64encode(crypto.sha256(response_transit_key_bytes)[:16])

    # Build request - include public key so receiver can derive same bloom salt
    # even before peer_shared is validated (key-based connection bootstrap)
    request_data = {
        'type': 'sync',
        'peer_id': from_peer_id,
        'signed_by': from_peer_shared_id,
        'requester_public_key': crypto.b64encode(requester_public_key),  # For bloom salt derivation
        'address': '127.0.0.1:8000',
        'window_id': window_id,
        'window_min': window_min,
        'window_max': window_max,
        'bloom': crypto.b64encode(bloom_filter),
        'response_transit_key_id': response_transit_key_id,
        'response_transit_key': crypto.b64encode(response_transit_key_bytes),
        'created_at': t_ms
    }

    # Sign and wrap
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
    signed_request = crypto.sign_event(request_data, private_key)
    canonical = crypto.canonicalize_json(signed_request)

    to_key = {
        'id': crypto.b64decode(their_transit_key_id),
        'key': their_transit_key,
        'type': 'symmetric'
    }
    request_blob = crypto.wrap(canonical, to_key, db)

    queues.incoming.add(request_blob, t_ms, db)
    log.info(f"send_request_to_connection: sent window={window_id} to_key={their_transit_key_id[:10]}...")


def send_request(to_peer_shared_id: str, from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send bloom-based sync request to peer for specific window.

    Args:
        to_peer_shared_id: Recipient's peer_shared_id (public identity)
        from_peer_id: Sender's local peer_id
        from_peer_shared_id: Sender's peer_shared_id (public identity)
        t_ms: Timestamp
        db: Database connection
    """

    log.debug(f"[SEND_REQUEST_ENTRY] from={from_peer_id[:20]}... to={to_peer_shared_id[:20]}...")

    # Get next window to sync
    window_id, w_param = get_next_window(from_peer_id, to_peer_shared_id, t_ms, db)
    log.debug(f"[SEND_REQUEST_WINDOW] from={from_peer_id[:20]}... window_id={window_id}, w_param={w_param}")
    log.info(f"send_request: from={from_peer_id[:10]}... to={to_peer_shared_id[:10]}... window_id={window_id}, w_param={w_param}")

    # Use SyncWindow to compute storage window range for this query window
    window = sync_window.SyncWindow(w=w_param, query_window_id=window_id)
    window_min, window_max = window.get_storage_window_range()

    # Query events the requester has seen (can share) in this window
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    my_events_in_window = safedb.query(
        """SELECT event_id, window_id FROM shareable_events
           WHERE can_share_peer_id = ?
             AND window_id >= ?
             AND window_id < ?
           ORDER BY recorded_at ASC""",
        (from_peer_id, window_min, window_max)
    )

    # Debug: Log which events are included in bloom
    if len(my_events_in_window) > 0:
        log.debug(f"[SEND_REQUEST_EVENTS] from={from_peer_id[:10]}... window={window_id} range={window_min}-{window_max}")
        for evt in my_events_in_window:
            log.debug(f"[SEND_REQUEST_EVENTS]   - event={evt['event_id'][:20]}... storage_window={evt['window_id']}")

    # Build list of event_id bytes for bloom
    event_id_bytes_list = [crypto.b64decode(row['event_id']) for row in my_events_in_window]

    # Derive salt for this window (from our own peer public key)
    # Use local peer key - we always have it, and we include it in request for receiver
    requester_public_key = peer.get_public_key(from_peer_id, from_peer_id, db)
    salt = derive_salt(requester_public_key, window_id)
    log.debug(f"[BLOOM_CREATE] from={from_peer_id[:10]}... from_peer_shared={from_peer_shared_id[:20]}... pubkey_for_salt={crypto.b64encode(requester_public_key)[:20]}...")

    # Create bloom filter of events requester HAS
    bloom_filter = create_bloom(event_id_bytes_list, salt)

    # Debug: Log bloom creation
    bits_set = bin(int.from_bytes(bloom_filter, 'big')).count('1')
    log.debug(f"[SEND_REQUEST_BLOOM] from={from_peer_id[:10]}... window={window_id} events_in_bloom={len(event_id_bytes_list)} bits_set={bits_set}/512")

    # Get established connection - sync uses the connection module's keys
    conn = conn_module.get_connection_by_peer(from_peer_id, to_peer_shared_id, t_ms, db)

    if conn and conn.can_send():
        # Use established connection - our_key for responses, their_key for sending
        response_key_id = conn.connection_id
        response_key_bytes = conn.our_key
        to_key = {
            'id': crypto.b64decode(conn.their_connection_id)[:16],
            'key': conn.their_key,
            'type': 'symmetric'
        }
        log.info(f"send_request: using established connection with {to_peer_shared_id[:20]}...")
    else:
        # No established connection - can't sync yet
        # Connection module will establish connections, then we can sync
        log.debug(f"send_request: no established connection to {to_peer_shared_id[:20]}..., skipping")
        return

    # Build sync request with connection's response key info
    request_data = {
        'type': 'sync',
        'peer_id': from_peer_id,
        'signed_by': from_peer_shared_id,  # Include so recipient knows which events to send
        'requester_public_key': crypto.b64encode(requester_public_key),  # For bloom salt derivation
        'address': '127.0.0.1:8000',
        'window_id': window_id,  # Which window we're requesting (for salt derivation and state tracking)
        'window_min': window_min,  # Concrete storage window range start
        'window_max': window_max,  # Concrete storage window range end
        'bloom': crypto.b64encode(bloom_filter),  # Bloom of events requester HAS
        'response_transit_key_id': response_key_id,  # Connection ID for routing responses
        'response_transit_key': crypto.b64encode(response_key_bytes),  # Key for wrapping responses
        'created_at': t_ms
    }

    # Sign the request
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
    signed_request = crypto.sign_event(request_data, private_key)

    # Store as signed plaintext
    canonical = crypto.canonicalize_json(signed_request)

    request_blob = crypto.wrap(canonical, to_key, db)

    # simulate sending - add to incoming queue
    queues.incoming.add(request_blob, t_ms, db)

    # Mark window as synced (optimistically - in production might wait for response)
    mark_window_synced(from_peer_id, to_peer_shared_id, window_id, t_ms, db)


def project(sync_event_id: str, recorded_by: str, recorded_at: int, db: Any, sync_data: dict | None = None) -> None:
    """Handle sync request by sending bloom-filtered response.

    Can be called either:
    - With sync_data=None (loads from store) - for recorded sync events
    - With sync_data dict (from ephemeral processing) - for directly handled sync events

    Args:
        sync_event_id: ID of the sync event
        recorded_by: Which peer recorded this event
        recorded_at: When they recorded it
        db: Database connection
        sync_data: Optional parsed sync request data. If None, loads from store.
    """

    if sync_data is None:
        # Load from store (non-ephemeral case)
        log.debug(f"[SYNC_PROJECT] sync_id={sync_event_id[:20]}... recorded_by={recorded_by[:10]}...")
        sync_blob = store.get(sync_event_id, db)
        if not sync_blob:
            log.info(f"sync blob not found in store")
            return
        sync_data = crypto.parse_json(sync_blob)

    _project_sync_event(sync_event_id, sync_data, recorded_by, recorded_at, db)


def _project_sync_event(sync_event_id: str, sync_data: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Internal function to handle sync request logic (shared between ephemeral and stored).

    STUB IMPLEMENTATION: Responds with ALL shareable events via connection.send().
    Future: Replace with negentropy range-based set reconciliation.
    """
    log.debug(f"[SYNC_PROJECT] sync_id={sync_event_id[:20]}... recorded_by={recorded_by[:10]}...")

    # Authentication: try signature first, fall back to implicit auth via connection
    sig_verified = crypto.verify_signed_by_peer_shared(sync_data, recorded_by, db)

    if not sig_verified:
        # Signature verification failed - this is OK if they authenticated via connection.
        # With connections, if they decrypted our key to send this request,
        # they're implicitly authenticated.
        log.debug(f"[SYNC_PROJECT] signature verification failed, accepting via implicit auth (connection)")

    # Extract requester info
    requester_peer_id = sync_data.get('peer_id')
    requester_peer_shared_id = sync_data.get('signed_by')
    from_connection_id = sync_data.get('from_connection_id')

    log.info(f"sync.project() processing sync request from connection={from_connection_id[:20] if from_connection_id else 'None'}...")

    if not requester_peer_id or not requester_peer_shared_id:
        log.info(f"Invalid sync request: missing requester info")
        return

    # Find the connection to respond on
    # The from_connection_id is their connection_id, which is our their_connection_id
    safedb = create_safe_db(db, recorded_by=recorded_by)

    if from_connection_id:
        # New style: use from_connection_id to find our connection
        conn_row = safedb.query_one("""
            SELECT connection_id, their_key FROM connections
            WHERE their_connection_id = ? AND recorded_by = ?
        """, (from_connection_id, recorded_by))
    else:
        # Fallback: find connection by peer_shared_id
        conn_row = safedb.query_one("""
            SELECT connection_id, their_key FROM connections
            WHERE peer_shared_id = ? AND recorded_by = ? AND their_key IS NOT NULL
            ORDER BY last_handshake_ms DESC LIMIT 1
        """, (requester_peer_shared_id, recorded_by))

    if not conn_row or not conn_row['their_key']:
        log.warning(f"[SYNC_PROJECT] no active connection to respond on for {requester_peer_shared_id[:20]}...")
        return

    our_connection_id = conn_row['connection_id']
    log.debug(f"[SYNC_PROJECT] found connection {our_connection_id[:20]}... to respond on")

    # STUB: Send ALL shareable events
    shareable_rows = safedb.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (recorded_by,)
    )

    log.info(f"[SYNC_RESPONSE_STUB] sending {len(shareable_rows)} events to {requester_peer_shared_id[:20]}...")

    sent_count = 0
    for row in shareable_rows:
        event_id = row['event_id']
        try:
            event_blob = safedb.get_shareable_blob(event_id)
            if conn_module.send(recorded_by, our_connection_id, event_blob, recorded_at, db):
                sent_count += 1
        except Exception as e:
            log.warning(f"sync_response: failed to send {event_id[:20]}...: {e}")

    log.info(f"[SYNC_RESPONSE_STUB] sent {sent_count}/{len(shareable_rows)} events")

    # Update sync state
    unsafedb = create_unsafe_db(db)
    sync_state_exists = unsafedb.query_one(
        "SELECT 1 FROM sync_state_ephemeral WHERE from_peer_id = ? AND to_peer_id = ?",
        (recorded_by, requester_peer_shared_id)
    )
    if not sync_state_exists:
        update_sync_state(recorded_by, requester_peer_shared_id, 0, 1, 0, recorded_at, db)



def send_response(to_peer_id: str, to_peer_shared_id: str, from_peer_id: str, transit_key_dict: dict[str, Any],
                  window_id: int, window_min: int, window_max: int, bloom_filter: bytes, requester_public_key: bytes,
                  t_ms: int, db: Any) -> None:
    """Send bloom-filtered sync response.

    Args:
        to_peer_id: Requester's peer_id (for logging)
        to_peer_shared_id: Requester's peer_shared_id (unused, kept for API compatibility)
        from_peer_id: Responder's peer_id (which peer is sending the response)
        transit_key_dict: Transit key dict from the sync request
        window_id: Window ID being synced (for salt derivation)
        window_min: Storage window range start
        window_max: Storage window range end
        bloom_filter: Bloom filter of events requester HAS
        requester_public_key: Requester's public key (for deriving salt)
        t_ms: Current timestamp
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=from_peer_id)

    log.debug(f"[SYNC_RESPONSE] from={from_peer_id[:10]}... to={to_peer_id[:10]}... window={window_id} range={window_min}-{window_max}")

    # Query random candidates to share (LIMIT to avoid O(n) scans for large event counts)
    MAX_CANDIDATES = 2000
    shareable_rows = safedb.query(
        """SELECT event_id FROM shareable_events
           WHERE can_share_peer_id = ?
             AND window_id >= ?
             AND window_id < ?
           ORDER BY RANDOM()
           LIMIT ?""",
        (from_peer_id, window_min, window_max, MAX_CANDIDATES)
    )
    log.debug(f"[SYNC_RESPONSE] found={len(shareable_rows)}_candidate_events from={from_peer_id[:10]}...")

    for row in shareable_rows:
        log.debug(f"[SYNC_RESPONSE]   candidate event={row['event_id'][:20]}...")

    # Derive salt for bloom checking (same salt requester used)
    salt = derive_salt(requester_public_key, window_id)
    log.debug(f"[BLOOM_CHECK] to={to_peer_id[:10]}... requester_pubkey_for_salt={crypto.b64encode(requester_public_key)[:20]}...")

    # Debug: Log bloom filter stats
    bits_set = bin(int.from_bytes(bloom_filter, 'big')).count('1')
    log.debug(f"[SYNC_RESPONSE] bloom_filter_bits_set={bits_set}/512 bloom_hex={bloom_filter.hex()[:40]}...")

    # Filter events using bloom: send only events that FAIL bloom check
    # (requester doesn't have them)
    events_to_send = []
    peer_shared_seen = 0
    peer_shared_will_send = 0
    for row in shareable_rows:
        event_id_str = row['event_id']
        event_id_bytes = crypto.b64decode(event_id_str)

        # Check if event is in requester's bloom
        in_bloom = check_bloom(event_id_bytes, bloom_filter, salt)

        if not in_bloom:
            # Event NOT in bloom -> requester doesn't have it -> send it
            events_to_send.append(event_id_str)
            log.debug(f"[SYNC_RESPONSE] will_send event_id={event_id_str[:20]}... (not_in_bloom)")
        else:
            log.debug(f"[SYNC_RESPONSE] skipping event_id={event_id_str[:20]}... (in_bloom)")

        # Try to detect peer_shared for instrumentation
        try:
            evt_blob_dbg = safedb.get_shareable_blob(event_id_str)
            evt_json_dbg = crypto.parse_json(evt_blob_dbg)
            if evt_json_dbg.get('type') == 'peer_shared':
                peer_shared_seen += 1
                if not in_bloom:
                    peer_shared_will_send += 1
        except Exception:
            pass

    log.debug(f"[SYNC_RESPONSE] sending={len(events_to_send)}_events to={to_peer_id[:10]}...")
    log.info(f"[SYNC_RESPONSE_STATS] from={from_peer_id[:10]}... to={to_peer_id[:10]}... shareable={len(shareable_rows)} will_send={len(events_to_send)} peer_shared_seen={peer_shared_seen} peer_shared_will_send={peer_shared_will_send}")

    if len(events_to_send) == 0 and len(shareable_rows) > 0:
        log.debug(f"[SYNC_RESPONSE] WARNING: All {len(shareable_rows)} events were filtered by bloom! This suggests a bloom filter bug.")

    # Send filtered events
    for event_id in events_to_send:
        try:
            event_blob = safedb.get_shareable_blob(event_id)
        except Exception as e:
            log.warning(f"send_response: failed to get shareable blob for {event_id[:20]}...: {e}")
            continue

        # Log event type
        try:
            event_data = crypto.parse_json(event_blob)
            event_type = event_data.get('type', 'unknown')
            log.info(f"send_response: sending {event_type} event {event_id[:20]}...")
        except:
            log.info(f"send_response: sending encrypted event {event_id[:20]}...")

        # Double-wrap with transit key
        hint_for_wrapping = crypto.b64encode(transit_key_dict['id'])
        log.warning(f"[SYNC_RESPONSE] wrapping event={event_id[:20]}... plaintext_size={len(event_blob)}B with transit_key_hint={hint_for_wrapping} ({len(hint_for_wrapping)} chars)")
        log.debug(f"[SYNC_RESPONSE] wrapping event={event_id[:20]}... with transit_key_hint={hint_for_wrapping} ({len(hint_for_wrapping)} chars)")
        wrapped_blob = crypto.wrap(event_blob, transit_key_dict, db)
        log.warning(f"[SYNC_RESPONSE] wrapped result: wrapped_blob_size={len(wrapped_blob)}B")
        actual_hint_in_blob = crypto.b64encode(wrapped_blob[:16])
        log.debug(f"[SYNC_RESPONSE] wrapped blob hint={actual_hint_in_blob} ({len(actual_hint_in_blob)} chars), matches_expected={actual_hint_in_blob == hint_for_wrapping}")

        # Count blobs in queue before adding
        from db import create_unsafe_db
        unsafedb = create_unsafe_db(db)
        before_count = unsafedb.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")['cnt']
        queues.incoming.add(wrapped_blob, t_ms, db)
        after_count = unsafedb.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs")['cnt']
        log.debug(f"[SYNC_RESPONSE] added blob to incoming queue: before={before_count}, after={after_count}, hint={actual_hint_in_blob}")


# =============================================================================
# Convergence Detection Functions (Snapshot-based)
# =============================================================================

def take_sync_snapshot(db: Any) -> dict:
    """Take a snapshot of current sync state for convergence detection.

    Captures the current valid event counts for each local peer.
    Used to detect when sync has stabilized (no new events being validated).

    Returns:
        Snapshot dict with:
        - 'local_peers': list of peer_ids
        - 'valid_counts': {peer_id: count of valid events}
        - 'queue_size': incoming_blobs count
        - 'blocked_counts': {peer_id: count of blocked events}
    """
    unsafedb = create_unsafe_db(db)

    # Get all local peers
    local_peers = [row['peer_id'] for row in unsafedb.query("SELECT peer_id FROM local_peers")]

    # Count valid events per peer (valid_events is subjective)
    valid_counts = {}
    for peer_id in local_peers:
        safedb = create_safe_db(db, recorded_by=peer_id)
        count = safedb.query_one(
            "SELECT COUNT(*) as count FROM valid_events WHERE recorded_by = ?",
            (peer_id,)
        )
        valid_counts[peer_id] = count['count'] if count else 0

    # Count blocked events per peer
    blocked_counts = {}
    for peer_id in local_peers:
        safedb = create_safe_db(db, recorded_by=peer_id)
        count = safedb.query_one(
            "SELECT COUNT(*) as count FROM blocked_events_ephemeral WHERE recorded_by = ?",
            (peer_id,)
        )
        blocked_counts[peer_id] = count['count'] if count else 0

    # Queue size
    queue = unsafedb.query_one("SELECT COUNT(*) as count FROM incoming_blobs")

    return {
        'local_peers': local_peers,
        'valid_counts': valid_counts,
        'queue_size': queue['count'] if queue else 0,
        'blocked_counts': blocked_counts
    }


def check_sync_progress(db: Any, prev_snapshot: dict) -> dict:
    """Check if sync has made progress since previous snapshot.

    Compares current state to previous snapshot to detect sync activity.
    Progress is determined by queue changes only (not valid_events count)
    because valid_events grows from both sync AND local event creation.

    Args:
        db: Database connection
        prev_snapshot: Previous snapshot from take_sync_snapshot()

    Returns:
        Status dict with:
        - 'progressed': bool (True if queue changed since last check)
        - 'valid_deltas': {peer_id: new events validated since snapshot}
        - 'queue_size': current incoming_blobs count
        - 'blocked_count': total blocked events
        - 'total_valid': total valid events across all peers
    """
    current = take_sync_snapshot(db)

    # Calculate deltas (for informational purposes)
    valid_deltas = {}
    total_valid = 0
    for peer_id in current['local_peers']:
        prev_count = prev_snapshot['valid_counts'].get(peer_id, 0)
        curr_count = current['valid_counts'].get(peer_id, 0)
        valid_deltas[peer_id] = curr_count - prev_count
        total_valid += curr_count

    # Only track queue changes for progress detection
    # (valid_events grows from local events too, not just sync)
    # Compare totals, not per-peer dicts, to avoid false positives from
    # blocked events moving between peers
    queue_changed = current['queue_size'] != prev_snapshot['queue_size']
    prev_blocked_total = sum(prev_snapshot['blocked_counts'].values())
    total_blocked = sum(current['blocked_counts'].values())
    blocked_changed = total_blocked != prev_blocked_total

    return {
        'progressed': queue_changed or blocked_changed,
        'valid_deltas': valid_deltas,
        'queue_size': current['queue_size'],
        'blocked_count': total_blocked,
        'total_valid': total_valid,
        'snapshot': current  # Include for next comparison
    }
