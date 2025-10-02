"""Sync implementation with bloom-based window protocol."""
from typing import Any, Iterator
from events import first_seen, key, prekey, peer
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
    row = db.query_one(
        "SELECT last_window, w_param, total_events_seen FROM sync_state WHERE from_peer_id = ? AND to_peer_id = ?",
        (from_peer_id, to_peer_id)
    )
    if row:
        return {
            'last_window': row['last_window'],
            'w_param': row['w_param'],
            'total_events_seen': row['total_events_seen']
        }
    return {
        'last_window': -1,
        'w_param': DEFAULT_W,
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
    db.execute(
        """INSERT INTO sync_state (from_peer_id, to_peer_id, last_window, w_param, total_events_seen, updated_at)
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
    """Mark window as synced and increase w_param if events exceed W × 450 threshold."""
    state = get_sync_state(from_peer_id, to_peer_id, t_ms, db)
    state['last_window'] = window_id

    # Dynamic w adjustment based on event count
    current_w = state['w_param']
    current_windows = compute_window_count(current_w)
    threshold = current_windows * EVENTS_PER_WINDOW_TARGET
    if state['total_events_seen'] > threshold:
        state['w_param'] = current_w + 1

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


def add_shareable_event(event_id: str, peer_id: str, created_at: int, db: Any) -> None:
    """Add shareable event to table with computed window_id."""
    event_id_bytes = crypto.b64decode(event_id)
    window_id = compute_storage_window_id(event_id_bytes)
    db.execute(
        """INSERT OR IGNORE INTO shareable_events (event_id, peer_id, created_at, window_id)
           VALUES (?, ?, ?, ?)""",
        (event_id, peer_id, created_at, window_id)
    )


def unwrap_and_store(blob: bytes, t_ms: int, db: Any) -> str:
    """Unwrap transit blob, store event, create first_seen; returns first_seen_id or empty string."""
    import logging
    log = logging.getLogger(__name__)

    hint = key.extract_id(blob)
    hint_b64 = crypto.b64encode(hint)

    # For incoming blobs: seen_by_peer_id = peer who owns the transit_key (the receiving peer)
    seen_by_peer_id = key.get_peer_id_for_key(hint_b64, db)

    if not seen_by_peer_id:
        log.info(f"Skipping storage for blob with id {hint_b64}: transit key not found (peer unknown)")
        return ""

    unwrapped_blob, missing_keys = crypto.unwrap(blob, db)
    if unwrapped_blob is None:
        if missing_keys:
            log.info(f"Skipping storage for blob with id {hint_b64}: missing keys {missing_keys}")
        else:
            log.info(f"Skipping storage for blob with id {hint_b64}: unwrap failed")
        return ""

    # Store the unwrapped event blob
    event_id = store.blob(unwrapped_blob, t_ms, True, db)

    # Create first_seen event for it (don't auto-project yet)
    first_seen_id = first_seen.create(event_id, seen_by_peer_id, t_ms, db, True)

    return first_seen_id


def receive(batch_size: int, t_ms: int, db: Any) -> None:
    """Receive and process a batch of incoming transit blobs."""
    import logging
    log = logging.getLogger(__name__)

    transit_blobs = queues.incoming.drain(batch_size, db)
    log.info(f"Drained {len(transit_blobs)} blobs from incoming queue")

    new_first_seen_ids = [unwrap_and_store(blob, t_ms, db) for blob in transit_blobs]
    log.info(f"Unwrapped {len(new_first_seen_ids)} blobs, got first_seen_ids: {new_first_seen_ids}")

    # Filter out empty strings (failed unwraps)
    valid_first_seen_ids = [id for id in new_first_seen_ids if id]
    log.info(f"Valid first_seen_ids to project: {valid_first_seen_ids}")

    first_seen.project_ids(valid_first_seen_ids, db)

    # Event-driven unblocking now happens automatically in first_seen.project()
    # via queues.blocked.notify_event_valid() - no need for scan-all loop

    db.commit()


def send_requests(from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send sync requests to all peers."""
    peer_rows = db.query("SELECT peer_id FROM peers WHERE peer_id != ?", (from_peer_id,))
    for row in peer_rows:
        send_request(row['peer_id'], from_peer_id, from_peer_shared_id, t_ms, db)
    db.commit()

def send_request(to_peer_id: str, from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send bloom-based sync request to peer for specific window."""
    # Get next window to sync
    window_id, w_param = get_next_window(from_peer_id, to_peer_id, t_ms, db)

    # Convert storage window_ids to query window_ids for this w_param
    window_min = window_id << (STORAGE_W - w_param)
    window_max = (window_id + 1) << (STORAGE_W - w_param)

    # Query events requester HAS in this window (created by requester)
    my_events_in_window = db.query(
        """SELECT event_id FROM shareable_events
           WHERE peer_id = ?
             AND window_id >= ?
             AND window_id < ?
           ORDER BY created_at ASC""",
        (from_peer_shared_id, window_min, window_max)
    )

    # Build list of event_id bytes for bloom
    event_id_bytes_list = [crypto.b64decode(row['event_id']) for row in my_events_in_window]

    # Derive salt for this window (from requester's peer public key)
    requester_public_key = peer.get_public_key(from_peer_id, db)
    salt = derive_salt(requester_public_key, window_id)

    # Create bloom filter of events requester HAS
    bloom_filter = create_bloom(event_id_bytes_list, salt)

    # Create a transit key for the response (owned by requester so they can decrypt response)
    response_transit_key_id = key.create(from_peer_id, t_ms, db)
    response_transit_key = key.get_key(response_transit_key_id, db)

    # Encode transit key for JSON serialization (recipient needs the actual key to wrap responses)
    request_data = {
        'type': 'sync',
        'peer_id': from_peer_id,
        'peer_shared_id': from_peer_shared_id,  # Include so recipient knows which events to send
        'address': '127.0.0.1:8000',
        'window_id': window_id,  # Which window we're requesting
        'w_param': w_param,  # Window parameter (for converting storage window_ids)
        'bloom': crypto.b64encode(bloom_filter),  # Bloom of events requester HAS
        'transit_key': {
            'id': crypto.b64encode(response_transit_key['id']),
            'key': crypto.b64encode(response_transit_key['key']),
            'type': response_transit_key['type']
        },
        'created_at': t_ms
    }

    # Sign the request
    private_key = peer.get_private_key(from_peer_id, db)
    signed_request = crypto.sign_event(request_data, private_key)

    # Wrap with recipient's prekey
    to_key = prekey.get_transit_prekey_for_peer(to_peer_id, db)
    canonical = crypto.canonicalize_json(signed_request)
    request_blob = crypto.wrap(canonical, to_key, db)

    # simulate sending - add to incoming queue
    queues.incoming.add(request_blob, t_ms, db)

    # Mark window as synced (optimistically - in production might wait for response)
    mark_window_synced(from_peer_id, to_peer_id, window_id, t_ms, db)


def receive_request(request_blob: bytes, db: Any) -> dict[str, Any] | None:
    """Receive and verify a sync request. Returns request data if valid, None otherwise."""
    # Unwrap the request
    unwrapped = crypto.unwrap(request_blob, db)
    if not unwrapped:
        return None

    # Parse JSON
    request_data = crypto.parse_json(unwrapped)

    # Verify signature - get public key from peer_id
    peer_id = request_data.get('peer_id')
    public_key = peer.get_public_key(peer_id, db)
    if not crypto.verify_event(request_data, public_key):
        return None  # Reject unsigned or invalid signature

    return request_data


def project(sync_event_id: str, seen_by_peer_id: str, received_at: int, db: Any) -> None:
    """Handle sync request by sending bloom-filtered response."""
    import logging
    log = logging.getLogger(__name__)
    log.info(f"sync.project() called for event {sync_event_id} seen by {seen_by_peer_id}")

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
    w_param = sync_data.get('w_param')
    bloom_b64 = sync_data.get('bloom')

    if not requester_peer_id or not requester_peer_shared_id or not transit_key_encoded:
        return  # Invalid sync request

    if window_id is None or w_param is None or not bloom_b64:
        log.info(f"Missing bloom/window data in sync request")
        return  # Invalid bloom-based sync request

    # Only respond to sync requests from peers we recognize (have their peer_shared)
    requester_known = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (requester_peer_shared_id, seen_by_peer_id)
    )
    if not requester_known:
        log.info(f"Rejecting sync request from unrecognized peer {requester_peer_shared_id}")
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
    requester_public_key = peer.get_public_key(requester_peer_id, db)

    # Send bloom-filtered response
    send_response(
        requester_peer_id,
        requester_peer_shared_id,
        seen_by_peer_id,
        transit_key_dict,
        window_id,
        w_param,
        bloom_filter,
        requester_public_key,
        received_at,
        db
    )


def send_response(to_peer_id: str, to_peer_shared_id: str, from_peer_id: str, transit_key_dict: dict[str, Any],
                  window_id: int, w_param: int, bloom_filter: bytes, requester_public_key: bytes,
                  t_ms: int, db: Any) -> None:
    """Send a bloom-filtered sync response for a specific window.

    Args:
        to_peer_id: Requester's peer_id (for logging)
        to_peer_shared_id: Requester's peer_shared_id (which events to send - events NOT created by requester)
        from_peer_id: Responder's peer_id (which peer is sending the response)
        transit_key_dict: Transit key dict from the sync request
        window_id: Window ID being synced
        w_param: Window parameter from request
        bloom_filter: Bloom filter of events requester HAS (64 bytes)
        requester_public_key: Requester's public key (for deriving salt)
        t_ms: Current timestamp
        db: Database connection
    """
    import logging
    log = logging.getLogger(__name__)

    # Get responder's own peer_shared_id deterministically
    from_peer_shared_id = ""
    candidate_rows = db.query(
        "SELECT peer_shared_id FROM peers_shared WHERE seen_by_peer_id = ?",
        (from_peer_id,)
    )
    for row in candidate_rows:
        ps_id = row['peer_shared_id']
        try:
            ps_blob = store.get(ps_id, db)
            if not ps_blob:
                continue
            ps_data = crypto.parse_json(ps_blob)
            if ps_data.get('type') == 'peer_shared' and ps_data.get('peer_id') == from_peer_id:
                from_peer_shared_id = ps_id
                break
        except Exception:
            continue

    if not from_peer_shared_id:
        log.info(f"No self peer_shared_id found for responder {from_peer_id}")
        return

    # Convert query window_id to storage window range
    window_min = window_id << (STORAGE_W - w_param)
    window_max = (window_id + 1) << (STORAGE_W - w_param)

    # Query shareable events in this window created by responder (NOT by requester)
    shareable_rows = db.query(
        """SELECT event_id FROM shareable_events
           WHERE peer_id = ?
             AND peer_id != ?
             AND window_id >= ?
             AND window_id < ?
           ORDER BY created_at ASC""",
        (from_peer_shared_id, to_peer_shared_id, window_min, window_max)
    )

    log.info(f"Found {len(shareable_rows)} candidate events in window {window_id}")

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

    log.info(f"Sending {len(events_to_send)} events after bloom filtering (FPR caused {len(shareable_rows) - len(events_to_send)} to be skipped)")

    # Send filtered events
    for event_id in events_to_send:
        event_blob = store.get(event_id, db)
        if not event_blob:
            continue

        # Double-wrap with transit key
        wrapped_blob = crypto.wrap(event_blob, transit_key_dict, db)
        queues.incoming.add(wrapped_blob, t_ms, db)
