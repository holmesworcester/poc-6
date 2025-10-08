"""Sync implementation with bloom-based window protocol."""
from typing import Any, Iterator
from events import recorded, key, prekey, peer
from db import create_safe_db, create_unsafe_db
import queues
import crypto
import store
import hashlib
import struct

# Bloom filter parameters
BLOOM_SIZE_BITS = 512  # 512 bits = 64 bytes
BLOOM_SIZE_BYTES = 64
K_HASHES = 5  # Number of hash functions

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

    # Start with w=1 (2 windows) for small networks
    # This will auto-adjust upward as events are synced
    return {
        'last_window': -1,
        'w_param': 1,
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
    import logging
    log = logging.getLogger(__name__)

    event_id_bytes = crypto.b64decode(event_id)
    window_id = compute_storage_window_id(event_id_bytes)

    log.info(f"add_shareable_event: event={event_id[:20]}..., can_share_peer_id={can_share_peer_id[:20]}..., window={window_id}")

    # Map parameter to column: can_share_peer_id -> can_share_peer_id
    safedb = create_safe_db(db, recorded_by=can_share_peer_id)
    safedb.execute(
        """INSERT OR IGNORE INTO shareable_events (event_id, can_share_peer_id, created_at, recorded_at, window_id)
           VALUES (?, ?, ?, ?, ?)""",
        (event_id, can_share_peer_id, created_at, recorded_at, window_id)
    )


def unwrap_and_store(blob: bytes, t_ms: int, db: Any) -> list[str]:
    """Unwrap transit blob, store event, create recorded events for all peers with access.

    Edge case: If multiple local peers have the same key (e.g., two peers in the same network
    both accepted the same invite), this creates a separate recorded event for each peer.

    Returns:
        List of recorded_ids (one per peer who can decrypt), or empty list if unwrap fails
    """
    import logging
    log = logging.getLogger(__name__)

    hint = key.extract_id(blob)
    hint_b64 = crypto.b64encode(hint)

    # For incoming blobs: get ALL peers who own this transit_key
    # This handles the edge case where multiple peers have the same key
    recorded_by_peers = key.get_peer_ids_for_key(hint_b64, db)

    log.info(f"unwrap_and_store: hint={hint_b64[:20]}..., peers_who_can_decrypt={[p[:20]+'...' for p in recorded_by_peers]}")

    if not recorded_by_peers:
        log.info(f"Skipping storage for blob with id {hint_b64}: transit key not found (peer unknown)")
        return []

    # Try to unwrap with each peer who has access (for prekeys, only owner can decrypt)
    unwrapped_blob = None
    for peer_id in recorded_by_peers:
        unwrapped_blob, missing_keys = crypto.unwrap(blob, peer_id, db)
        if unwrapped_blob is not None:
            break

    if unwrapped_blob is None:
        if missing_keys:
            log.info(f"Skipping storage for blob with id {hint_b64}: missing keys {missing_keys}")
        else:
            log.info(f"Skipping storage for blob with id {hint_b64}: unwrap failed for all {len(recorded_by_peers)} peers")
        return []

    # Store the unwrapped event blob (once)
    event_id = store.blob(unwrapped_blob, t_ms, True, db)

    # Create recorded event for EACH peer who can decrypt
    # This ensures each peer gets their own view of when they recorded the event
    recorded_ids = []
    for recorded_by in recorded_by_peers:
        recorded_id = recorded.create(event_id, recorded_by, t_ms, db, True)
        recorded_ids.append(recorded_id)
        log.info(f"Created recorded event {recorded_id} for peer {recorded_by}")

    return recorded_ids


def receive(batch_size: int, t_ms: int, db: Any) -> None:
    """Receive and process a batch of incoming transit blobs."""
    import logging
    log = logging.getLogger(__name__)

    transit_blobs = queues.incoming.drain(batch_size, db)
    log.info(f"Drained {len(transit_blobs)} blobs from incoming queue")

    # unwrap_and_store now returns a list of recorded_ids (one per peer who can decrypt)
    new_recorded_id_lists = [unwrap_and_store(blob, t_ms, db) for blob in transit_blobs]
    log.info(f"Unwrapped {len(new_recorded_id_lists)} blobs, got recorded_id lists: {new_recorded_id_lists}")

    # Flatten the list of lists to get all recorded_ids
    valid_recorded_ids = [id for id_list in new_recorded_id_lists for id in id_list]
    log.info(f"Valid recorded_ids to project: {valid_recorded_ids}")

    recorded.project_ids(valid_recorded_ids, db)

    # Event-driven unblocking now happens automatically in recorded.project()
    # via queues.blocked.notify_event_valid() - no need for scan-all loop

    db.commit()


def sync_all(t_ms: int, db: Any) -> None:
    """All local peers send sync requests to all peers they've seen."""
    import logging
    log = logging.getLogger(__name__)

    # Query all local peers
    unsafedb = create_unsafe_db(db)
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']

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
            log.debug(f"sync_all: skipping peer {peer_id[:10]}... (no peer_shared_id)")
            continue  # Skip if we can't find peer_shared_id

        log.debug(f"sync_all: peer {peer_id[:10]}... sending requests")
        # Send sync requests from this peer to all peers they've seen
        send_requests(peer_id, peer_shared_id, t_ms, db)


def send_requests(from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send sync requests to all peers this peer has seen."""
    # Query all peer_shared events seen by this peer
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    peer_shared_rows = safedb.query(
        "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
        (from_peer_id,)
    )

    for row in peer_shared_rows:
        ps_id = row['peer_shared_id']
        # Skip self (don't sync with yourself)
        if ps_id == from_peer_shared_id:
            continue

        # Get the peer_id from the peer_shared blob (for single-DB simulation)
        try:
            ps_blob = store.get(ps_id, db)
            if not ps_blob:
                continue
            ps_data = crypto.parse_json(ps_blob)
            to_peer_id = ps_data.get('peer_id')
            if not to_peer_id:
                continue
            send_request(to_peer_id, from_peer_id, from_peer_shared_id, t_ms, db)
        except Exception:
            continue

    db.commit()

def send_request(to_peer_id: str, from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send bloom-based sync request to peer for specific window."""
    import logging
    log = logging.getLogger(__name__)

    # Get next window to sync
    window_id, w_param = get_next_window(from_peer_id, to_peer_id, t_ms, db)
    log.info(f"send_request: from={from_peer_id[:10]}... to={to_peer_id[:10]}... window_id={window_id}, w_param={w_param}")

    # Convert storage window_ids to query window_ids for this w_param
    window_min = window_id << (STORAGE_W - w_param)
    window_max = (window_id + 1) << (STORAGE_W - w_param)

    # Query events the requester has seen (can share) in this window
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    my_events_in_window = safedb.query(
        """SELECT event_id FROM shareable_events
           WHERE can_share_peer_id = ?
             AND window_id >= ?
             AND window_id < ?
           ORDER BY created_at ASC""",
        (from_peer_id, window_min, window_max)
    )

    # Build list of event_id bytes for bloom
    event_id_bytes_list = [crypto.b64decode(row['event_id']) for row in my_events_in_window]

    # Derive salt for this window (from requester's peer public key)
    requester_public_key = peer.get_public_key(from_peer_id, from_peer_id, db)
    salt = derive_salt(requester_public_key, window_id)

    # Create bloom filter of events requester HAS
    bloom_filter = create_bloom(event_id_bytes_list, salt)

    # Create a transit key for the response (owned by requester so they can decrypt response)
    response_transit_key_id = key.create(from_peer_id, t_ms, db)
    response_transit_key = key.get_key(response_transit_key_id, from_peer_id, db)

    # Encode transit key for JSON serialization (recipient needs the actual key to wrap responses)
    request_data = {
        'type': 'sync',
        'peer_id': from_peer_id,
        'peer_shared_id': from_peer_shared_id,  # Include so recipient knows which events to send
        'address': '127.0.0.1:8000',
        'window_id': window_id,  # Which window we're requesting (for salt derivation and state tracking)
        'window_min': window_min,  # Concrete storage window range start
        'window_max': window_max,  # Concrete storage window range end
        'bloom': crypto.b64encode(bloom_filter),  # Bloom of events requester HAS
        'transit_key': {
            'id': crypto.b64encode(response_transit_key['id']),
            'key': crypto.b64encode(response_transit_key['key']),
            'type': response_transit_key['type']
        },
        'created_at': t_ms
    }

    # Sign the request
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
    signed_request = crypto.sign_event(request_data, private_key)

    # Wrap with recipient's prekey
    to_key = prekey.get_transit_prekey_for_peer(to_peer_id, from_peer_id, db)
    canonical = crypto.canonicalize_json(signed_request)
    request_blob = crypto.wrap(canonical, to_key, db)

    # simulate sending - add to incoming queue
    queues.incoming.add(request_blob, t_ms, db)

    # Mark window as synced (optimistically - in production might wait for response)
    mark_window_synced(from_peer_id, to_peer_id, window_id, t_ms, db)


def project(sync_event_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Handle sync request by sending bloom-filtered response."""
    import logging
    log = logging.getLogger(__name__)
    log.info(f"sync.project() called for event {sync_event_id} recorded by {recorded_by}")

    # Get the sync event data from store
    sync_blob = store.get(sync_event_id, db)
    if not sync_blob:
        log.info(f"sync blob not found in store")
        return

    # Parse sync request data
    sync_data = crypto.parse_json(sync_blob)

    # Extract requester info
    requester_peer_id = sync_data.get('peer_id')
    requester_peer_shared_id = sync_data.get('peer_shared_id')
    transit_key_encoded = sync_data.get('transit_key')
    window_id = sync_data.get('window_id')
    window_min = sync_data.get('window_min')
    window_max = sync_data.get('window_max')
    bloom_b64 = sync_data.get('bloom')

    log.info(f"sync.project() processing sync request: window_id={window_id}, window_range={window_min}-{window_max}")

    if not requester_peer_id or not requester_peer_shared_id or not transit_key_encoded:
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
        log.info(f"Rejecting sync request from unrecognized peer {requester_peer_shared_id} (will retry in next round)")
        return

    log.info(f"Accepting sync request from recognized peer {requester_peer_shared_id} for window {window_id}")

    # Decode transit_key from base64-encoded dict to bytes dict
    transit_key_dict = {
        'id': crypto.b64decode(transit_key_encoded['id']),
        'key': crypto.b64decode(transit_key_encoded['key']),
        'type': transit_key_encoded['type']
    }

    # Decode bloom filter
    bloom_filter = crypto.b64decode(bloom_b64)

    # Get requester's public key (for deriving bloom salt)
    # Use peer_shared since requester is a remote peer (not in local_peers)
    from events import peer_shared
    requester_public_key = peer_shared.get_public_key(requester_peer_shared_id, recorded_by, db)

    # Send bloom-filtered response
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
    import logging
    log = logging.getLogger(__name__)

    log.info(f"send_response: from_peer_id={from_peer_id[:10]}..., to_peer_id={to_peer_id[:10]}..., window={window_min}-{window_max}")

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
    log.info(f"send_response: found {len(shareable_rows)} shareable events for peer {from_peer_id[:10]}...")

    # Derive salt for bloom checking (same salt requester used)
    salt = derive_salt(requester_public_key, window_id)

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
            log.info(f"send_response: will send event {event_id_str[:20]}... (not in bloom)")
        else:
            log.info(f"send_response: skipping event {event_id_str[:20]}... (in bloom)")

    log.info(f"send_response: sending {len(events_to_send)} events to requester")

    # Send filtered events
    for event_id in events_to_send:
        try:
            event_blob = safedb.get_blob(event_id)
        except Exception as e:
            log.warning(f"send_response: failed to get blob for {event_id[:20]}...: {e}")
            continue

        # Log event type
        try:
            event_data = crypto.parse_json(event_blob)
            event_type = event_data.get('type', 'unknown')
            log.info(f"send_response: sending {event_type} event {event_id[:20]}...")
        except:
            log.info(f"send_response: sending encrypted event {event_id[:20]}...")

        # Double-wrap with transit key
        wrapped_blob = crypto.wrap(event_blob, transit_key_dict, db)
        queues.incoming.add(wrapped_blob, t_ms, db)
