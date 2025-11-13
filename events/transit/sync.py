"""Sync implementation with bloom-based window protocol."""
from typing import Any, Iterator
from events.transit import recorded, transit_key, transit_prekey
from events.identity import peer
from db import create_safe_db, create_unsafe_db
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
EPHEMERAL_EVENT_TYPES = {'sync', 'sync_file'}

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
    total_windows = compute_window_count(state['w_param'])
    next_window = (state['last_window'] + 1) % total_windows
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

    # Compute optimal w_param for this event count
    optimal_w = compute_w_for_event_count(total_events)
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
# Core Sync Functions
# ============================================================================


def add_shareable_event(event_id: str, can_share_peer_id: str, created_at: int, recorded_at: int, db: Any) -> None:
    """Add shareable event to table with computed window_id.

    Args:
        event_id: The event being marked as shareable
        can_share_peer_id: The peer who recorded/has this event and can share it (typically recorded_by)
        created_at: When the event was originally created
        recorded_at: When this peer recorded the event
        db: Database connection
    """

    event_id_bytes = crypto.b64decode(event_id)
    window_id = compute_storage_window_id(event_id_bytes)

    log.debug(f"add_shareable_event: event={event_id[:20]}..., peer={can_share_peer_id[:20]}..., window={window_id}")

    safedb = create_safe_db(db, recorded_by=can_share_peer_id)
    safedb.execute(
        """INSERT OR IGNORE INTO shareable_events (event_id, can_share_peer_id, created_at, recorded_at, window_id)
           VALUES (?, ?, ?, ?, ?)""",
        (event_id, can_share_peer_id, created_at, recorded_at, window_id)
    )


def route_blob_to_peers(blob: bytes, db: Any) -> list[str]:
    """Device-wide routing: determine which local peers can decrypt this blob.

    Checks transit keys (symmetric) and transit prekeys (asymmetric) to find
    all local peers who have the decryption key for this blob.

    Args:
        blob: Transit-wrapped blob with hint in first 16 bytes
        db: Database connection

    Returns:
        List of peer_ids who can decrypt this blob (empty if no keys found)
    """

    hint = blob[:16]
    hint_b64 = crypto.b64encode(hint)

    # Try transit keys first (symmetric)
    recorded_by_peers = transit_key.get_peer_ids_for_key(hint_b64, db)
    if recorded_by_peers:
        log.debug(f"route_blob_to_peers: routed to {len(recorded_by_peers)} peers via transit_key")
        return recorded_by_peers

    # Try transit prekeys (asymmetric)
    try:
        cursor = db._conn.execute(
            "SELECT DISTINCT recorded_by FROM transit_prekeys_shared WHERE transit_prekey_id = ?",
            (hint_b64,)
        )
        recorded_by_peers = [row[0] for row in cursor.fetchall()]
        if recorded_by_peers:
            log.debug(f"route_blob_to_peers: routed to {len(recorded_by_peers)} peers via transit_prekey")
    except Exception as e:
        log.warning(f"route_blob_to_peers: Failed to query transit_prekeys_shared: {e}")
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
        from events.transit import sync_file
        sync_file.project(event_id, recorded_by, t_ms, db, sync_file_data=event_data)

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

    # Phase 3: Bootstrap special case removed
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
        from core import network
        from events.network import address as address_module

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
    transit_blobs = queues.incoming.drain(batch_size, t_ms, db)
    log.info(f"sync.receive: processing {len(transit_blobs)} blobs")

    # unwrap_and_store returns list of recorded_ids (one per peer who can decrypt)
    new_recorded_id_lists = []
    for blob in transit_blobs:
        result = unwrap_and_store(blob, t_ms, db)
        new_recorded_id_lists.append(result)

    # Flatten and project all recorded events
    valid_recorded_ids = [id for id_list in new_recorded_id_lists for id in id_list]
    log.debug(f"sync.receive: projecting {len(valid_recorded_ids)} recorded events")

    recorded.project_ids(valid_recorded_ids, db)

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

        if not peer_shared_id:
            continue  # Skip if we can't find peer_shared_id

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
    from events.transit import sync_file

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
    """Send sync requests to all active connections.

    Uses the connection layer (sync_connections) instead of querying peers_shared directly.
    This follows the two-layer architecture: connections are established first via sync_connect,
    then sync operates on those established connections.
    """

    # Standardize encoding for logging
    if isinstance(from_peer_id, bytes):
        peer_id_str = crypto.b64encode(from_peer_id)
    else:
        peer_id_str = from_peer_id

    # Query active connections (device-wide, no recorded_by)
    unsafedb = create_unsafe_db(db)
    connection_rows = unsafedb.query(
        """SELECT peer_shared_id FROM sync_connections
           WHERE last_seen_ms + ttl_ms > ?""",
        (t_ms,)
    )

    connection_ids = [row['peer_shared_id'][:10] + '...' for row in connection_rows]
    log.warning(f"[SYNC_SEND] from_peer={peer_id_str[:10]}... connections={len(connection_rows)} ids={connection_ids}")

    for row in connection_rows:
        peer_shared_id = row['peer_shared_id']

        # Send sync request to this connected peer
        log.warning(f"[SYNC_REQUEST] from={peer_id_str[:10]}... to={peer_shared_id[:10]}...")
        send_request(peer_shared_id, from_peer_id, from_peer_shared_id, t_ms, db)

    db.commit()


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

    # Convert storage window_ids to query window_ids for this w_param
    window_min = window_id << (STORAGE_W - w_param)
    window_max = (window_id + 1) << (STORAGE_W - w_param)

    # Query events the requester has seen (can share) in this window
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    my_events_in_window = safedb.query(
        """SELECT event_id, window_id FROM shareable_events
           WHERE can_share_peer_id = ?
             AND window_id >= ?
             AND window_id < ?
           ORDER BY created_at ASC""",
        (from_peer_id, window_min, window_max)
    )

    # Debug: Log which events are included in bloom
    if len(my_events_in_window) > 0:
        log.debug(f"[SEND_REQUEST_EVENTS] from={from_peer_id[:10]}... window={window_id} range={window_min}-{window_max}")
        for evt in my_events_in_window:
            log.debug(f"[SEND_REQUEST_EVENTS]   - event={evt['event_id'][:20]}... storage_window={evt['window_id']}")

    # Build list of event_id bytes for bloom
    event_id_bytes_list = [crypto.b64decode(row['event_id']) for row in my_events_in_window]

    # Derive salt for this window (from requester's peer_shared public key)
    # IMPORTANT: Must use peer_shared public key, not local peer key, because responder
    # won't have access to requester's local peer table (only peers_shared table)
    from events.identity import peer_shared
    requester_public_key = peer_shared.get_public_key(from_peer_shared_id, from_peer_id, db)
    salt = derive_salt(requester_public_key, window_id)
    log.debug(f"[BLOOM_CREATE] from={from_peer_id[:10]}... from_peer_shared={from_peer_shared_id[:20]}... pubkey_for_salt={crypto.b64encode(requester_public_key)[:20]}...")

    # Create bloom filter of events requester HAS
    bloom_filter = create_bloom(event_id_bytes_list, salt)

    # Debug: Log bloom creation
    bits_set = bin(int.from_bytes(bloom_filter, 'big')).count('1')
    log.debug(f"[SEND_REQUEST_BLOOM] from={from_peer_id[:10]}... window={window_id} events_in_bloom={len(event_id_bytes_list)} bits_set={bits_set}/512")

    # Create a transit key for the response (owned by requester so they can decrypt response)
    response_transit_key_id = transit_key.create(from_peer_id, t_ms, db)

    # Get the raw key bytes from DB (don't use get_key() to avoid encoding round-trip)
    from db import create_unsafe_db
    unsafedb = create_unsafe_db(db)
    key_row = unsafedb.query_one("SELECT key FROM transit_keys WHERE key_id = ?", (response_transit_key_id,))
    if not key_row:
        raise ValueError(f"transit key not found after creation: {response_transit_key_id}")
    response_transit_key_bytes = key_row['key']

    log.debug(f"[SEND_REQUEST] from={from_peer_id[:10]}... created response_transit_key_id={response_transit_key_id} (len={len(response_transit_key_id)} chars)")

    # Flatten transit key fields for JSON serialization (recipient needs the actual key to wrap responses)
    request_data = {
        'type': 'sync',
        'peer_id': from_peer_id,
        'created_by': from_peer_shared_id,  # Include so recipient knows which events to send
        'address': '127.0.0.1:8000',
        'window_id': window_id,  # Which window we're requesting (for salt derivation and state tracking)
        'window_min': window_min,  # Concrete storage window range start
        'window_max': window_max,  # Concrete storage window range end
        'bloom': crypto.b64encode(bloom_filter),  # Bloom of events requester HAS
        'response_transit_key_id': response_transit_key_id,  # Base64 key ID for crypto hint
        'response_transit_key': crypto.b64encode(response_transit_key_bytes),  # Base64 key material
        'created_at': t_ms
    }
    log.debug(f"[SEND_REQUEST] serializing transit_key_id into request: {response_transit_key_id} (len={len(response_transit_key_id)})")

    # Sign the request
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
    signed_request = crypto.sign_event(request_data, private_key)

    # Store as signed plaintext
    canonical = crypto.canonicalize_json(signed_request)

    # Try to get established connection first (uses symmetric transit key)
    unsafedb = create_unsafe_db(db)
    conn = unsafedb.query_one("""
        SELECT response_transit_key_id, response_transit_key
        FROM sync_connections
        WHERE peer_shared_id = ?
          AND last_seen_ms + ttl_ms > ?
    """, (to_peer_shared_id, t_ms))

    if conn:
        # Use established connection's transit key
        to_key = {
            'id': crypto.b64decode(conn['response_transit_key_id']),
            'key': conn['response_transit_key'],
            'type': 'symmetric'
        }
        log.info(f"send_request: using established connection with {to_peer_shared_id[:20]}...")
    else:
        # Fall back to transit prekey for initial contact (asymmetric)
        to_key = transit_prekey.get_transit_prekey_for_peer(to_peer_shared_id, from_peer_id, db)
        if to_key:
            log.info(f"send_request: falling back to prekey for {to_peer_shared_id[:20]}... hint={crypto.b64encode(to_key['id'])[:30]}...")
        else:
            log.warning(f"send_request: NO CONNECTION OR PREKEY for {to_peer_shared_id[:20]}...")
            return

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
    """Internal function to handle sync request logic (shared between ephemeral and stored)."""
    log.debug(f"[SYNC_PROJECT] sync_id={sync_event_id[:20]}... recorded_by={recorded_by[:10]}...")

    # Verify signature using requester's peer_shared public key
    if not crypto.verify_signed_by_peer_shared(sync_data, recorded_by, db):
        log.warning(f"sync.project() signature verification failed or peer_shared not available yet")
        return

    # Extract requester info
    requester_peer_id = sync_data.get('peer_id')
    requester_peer_shared_id = sync_data.get('created_by')
    response_transit_key_id = sync_data.get('response_transit_key_id')
    response_transit_key_b64 = sync_data.get('response_transit_key')
    window_id = sync_data.get('window_id')
    window_min = sync_data.get('window_min')
    window_max = sync_data.get('window_max')
    bloom_b64 = sync_data.get('bloom')

    log.info(f"sync.project() processing sync request: window_id={window_id}, window_range={window_min}-{window_max}")

    if not requester_peer_id or not requester_peer_shared_id or not response_transit_key_id or not response_transit_key_b64:
        log.info(f"Invalid sync request: missing requester info")
        return  # Invalid sync request

    if window_id is None or window_min is None or window_max is None or not bloom_b64:
        log.info(f"Missing bloom/window data in sync request")
        return  # Invalid bloom-based sync request

    # Only respond to sync requests from peers we recognize (have their peer_shared valid)
    # If not valid, discard - the requester will send another sync request in the next round
    safedb = create_safe_db(db, recorded_by=recorded_by)
    requester_known = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (requester_peer_shared_id, recorded_by)
    )
    if not requester_known:
        log.debug(f"[SYNC_PROJECT] result=REJECTED requester={requester_peer_shared_id[:20]}... not_recognized_by={recorded_by[:10]}...")
        return

    log.debug(f"[SYNC_PROJECT] result=ACCEPTED requester={requester_peer_shared_id[:20]}... recognized_by={recorded_by[:10]}... window={window_id}")

    # Check if we've synced with this peer before
    unsafedb = create_unsafe_db(db)
    sync_state_exists = unsafedb.query_one(
        "SELECT 1 FROM sync_state_ephemeral WHERE from_peer_id = ? AND to_peer_id = ?",
        (recorded_by, requester_peer_shared_id)
    )

    # Build transit_key dict for wrapping responses
    # response_transit_key_id is already base64 string (key ID from DB)
    # Decode it to bytes for crypto hint
    transit_key_id_bytes = crypto.b64decode(response_transit_key_id)
    transit_key_dict = {
        'id': transit_key_id_bytes,
        'key': crypto.b64decode(response_transit_key_b64),
        'type': 'symmetric'
    }
    log.debug(f"[SYNC_PROJECT] extracted transit_key_id={response_transit_key_id} (len={len(response_transit_key_id)} chars, decoded to {len(transit_key_id_bytes)} bytes)")

    # Decode bloom filter
    bloom_filter = crypto.b64decode(bloom_b64)

    # Get requester's public key (for deriving bloom salt)
    # Use peer_shared since requester is a remote peer (not in local_peers)
    from events.identity import peer_shared
    requester_public_key = peer_shared.get_public_key(requester_peer_shared_id, recorded_by, db)

    # Always use normal bloom-filtered sync response (single window)
    log.debug(f"[SYNC_PROJECT] calling send_response with transit_key_hint={crypto.b64encode(transit_key_dict['id'])} ({len(crypto.b64encode(transit_key_dict['id']))} chars)")
    send_response(
        requester_peer_id,
        requester_peer_shared_id,
        recorded_by,
        transit_key_dict,
        window_id,
        window_min,
        window_max,
        bloom_filter,
        requester_public_key,
        recorded_at,
        db
    )

    # Initialize sync state if this is first sync
    if not sync_state_exists:
        update_sync_state(recorded_by, requester_peer_shared_id, 0, 1, 0, recorded_at, db)


def send_response(to_peer_id: str, to_peer_shared_id: str, from_peer_id: str, transit_key_dict: dict[str, Any],
                  window_id: int, window_min: int, window_max: int, bloom_filter: bytes, requester_public_key: bytes,
                  t_ms: int, db: Any) -> None:
    """Send a bloom-filtered sync response for a specific window.

    Args:
        to_peer_id: Requester's peer_id (for logging)
        to_peer_shared_id: Requester's peer_shared_id (unused, kept for API compatibility)
        from_peer_id: Responder's peer_id (which peer is sending the response)
        transit_key_dict: Transit key dict from the sync request
        window_id: Window ID being synced (for salt derivation)
        window_min: Storage window range start
        window_max: Storage window range end
        bloom_filter: Bloom filter of events requester HAS (64 bytes)
        requester_public_key: Requester's public key (for deriving salt)
        t_ms: Current timestamp
        db: Database connection
    """

    log.debug(f"[SYNC_RESPONSE] from={from_peer_id[:10]}... to={to_peer_id[:10]}... window={window_id} range={window_min}-{window_max}")

    # Query events the responder can share in this window
    # The bloom filter handles deduplication, so we don't need to exclude requester's events
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    shareable_rows = safedb.query(
        """SELECT event_id FROM shareable_events
           WHERE can_share_peer_id = ?
             AND window_id >= ?
             AND window_id < ?
           ORDER BY created_at ASC""",
        (from_peer_id, window_min, window_max)
    )
    log.debug(f"[SYNC_RESPONSE] found={len(shareable_rows)}_shareable_events from={from_peer_id[:10]}...")
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

    log.debug(f"[SYNC_RESPONSE] sending={len(events_to_send)}_events to={to_peer_id[:10]}...")

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
