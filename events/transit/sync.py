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

# Bloom filter parameters
BLOOM_SIZE_BITS = 512  # 512 bits = 64 bytes
BLOOM_SIZE_BYTES = 64
K_HASHES = 5  # Number of hash functions

# Event types that are sync protocol infrastructure (not stored in event log)
EPHEMERAL_EVENT_TYPES = {'sync'}

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
    import logging
    log = logging.getLogger(__name__)
    log.warning(f"[MARK_WINDOW_SYNCED] from_peer_id={from_peer_id[:20]}... to_peer_id={to_peer_id[:20]}... window_id={window_id}")
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

    hint = blob[:16]  # Extract hint from blob (first 16 bytes, already a hash)
    hint_b64 = crypto.b64encode(hint)

    log.warning(f"[UNWRAP_START] blob_size={len(blob)}B hint={hint_b64[:20]}...")

    # For incoming blobs: get ALL peers who can decrypt this blob
    # Try transit keys first (symmetric), then prekeys (asymmetric)
    recorded_by_peers = transit_key.get_peer_ids_for_key(hint_b64, db)
    log.warning(f"[UNWRAP_TRANSIT_KEY] hint={hint_b64[:20]}... result=found_{len(recorded_by_peers)}_peers peers={[p[:10]+'...' for p in recorded_by_peers]}")

    # If not a transit key, check if it's a prekey (for sync requests wrapped to prekeys)
    if not recorded_by_peers:
        # Check transit_prekeys table - hint is prekey_shared_id
        # Two cases: detached prekey (prekey_id = prekey_shared_id) or regular prekey (need to look up via transit_prekeys_shared)
        from db import create_unsafe_db, create_safe_db
        import store as store_module
        unsafedb = create_unsafe_db(db)

        # Try detached prekey first (prekey_id = prekey_shared_id)
        # Debug: list all prekeys in database
        # For asymmetric (prekey) wrapping, hint is transit_prekey_id (matches prekey_id in recipient's transit_prekeys table)
        # Query transit_prekeys_shared to find all peers who have this prekey in their subjective view
        # Note: transit_prekeys_shared is subjective, but we need to find ALL peers
        # Use raw SQL via Database._conn to bypass SafeDB scoping
        try:
            cursor = db._conn.execute(
                "SELECT DISTINCT recorded_by FROM transit_prekeys_shared WHERE transit_prekey_id = ?",
                (hint_b64,)
            )
            prekey_shared_rows = cursor.fetchall()
            recorded_by_peers = [row[0] for row in prekey_shared_rows]  # fetchall returns tuples

            if recorded_by_peers:
                log.info(f"unwrap_and_store: hint={hint_b64[:30]}..., found {len(recorded_by_peers)} peers via transit_prekey_id lookup: {[p[:20] for p in recorded_by_peers]}")
        except Exception as e:
            log.warning(f"unwrap_and_store: Failed to query transit_prekeys_shared: {e}")
            recorded_by_peers = []

        # If not found via prekeys, try symmetric transit_keys
        if not recorded_by_peers:
            # Hint might be a transit_key_id (symmetric wrapping)
            from events.transit import transit_key as transit_key_module
            transit_key_peer_ids = transit_key_module.get_peer_ids_for_key(hint_b64, db)
            recorded_by_peers = transit_key_peer_ids
            if recorded_by_peers:
                log.info(f"unwrap_and_store: hint={hint_b64[:30]}..., found {len(recorded_by_peers)} peers via transit_key lookup: {[p[:20] for p in recorded_by_peers]}")

    log.info(f"unwrap_and_store: hint={hint_b64[:20]}..., peers_who_can_decrypt={[p[:20]+'...' for p in recorded_by_peers]}")

    if not recorded_by_peers:
        log.info(f"Skipping storage for blob with id {hint_b64}: key/prekey not found (peer unknown)")
        return []

    # Try to unwrap with each peer who has access (for prekeys, only owner can decrypt)
    unwrapped_blob = None
    for peer_id in recorded_by_peers:
        unwrapped_blob, missing_keys = crypto.unwrap_transit(blob, peer_id, db)
        if unwrapped_blob is not None:
            break

    if unwrapped_blob is None:
        if missing_keys:
            log.info(f"Skipping storage for blob with id {hint_b64}: missing keys {missing_keys}")
        else:
            log.info(f"Skipping storage for blob with id {hint_b64}: unwrap failed for all {len(recorded_by_peers)} peers")
        return []

    # Check if this is a sync event (ephemeral protocol event)
    # Sync events should be processed immediately without storing in event log
    try:
        event_data = crypto.parse_json(unwrapped_blob)
        event_type = event_data.get('type')
        log.warning(f"[UNWRAP_PARSE] Parsed event type={event_type}, checking if ephemeral (types={EPHEMERAL_EVENT_TYPES})")

        if event_type in EPHEMERAL_EVENT_TYPES:
            # Handle sync events ephemerally - project directly without storing
            log.warning(f"[EPHEMERAL_SYNC] Processing ephemeral {event_type} event for {len(recorded_by_peers)} peers")
            log.warning(f"[EPHEMERAL_SYNC] recorded_by_peers={[p[:10]+'...' for p in recorded_by_peers]}")
            # Compute event_id same way as store.blob would (for consistency)
            event_id = crypto.b64encode(crypto.hash(unwrapped_blob))

            # Project sync event directly for each peer (without storing)
            for recorded_by in recorded_by_peers:
                log.warning(f"[EPHEMERAL_SYNC] Calling project_ephemeral for recorded_by={recorded_by[:10]}...")
                # Call project() directly with ephemeral event data
                from events.transit import sync as sync_module
                sync_module.project_ephemeral(event_id, event_data, recorded_by, t_ms, db)

                # Mark sync event as valid (same as recorded.project() does)
                # This is critical for sync protocol to track which sync requests have been processed
                from db import create_safe_db
                safedb = create_safe_db(db, recorded_by=recorded_by)
                safedb.execute(
                    "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
                    (event_id, recorded_by)
                )

            # Return empty list - no recorded events to project later
            return []
    except Exception as e:
        log.warning(f"[UNWRAP_PARSE_FAIL] Failed to parse as ephemeral event: {e}, continuing with normal storage")
        pass  # Not JSON or parse failed, continue with normal storage

    # Store the unwrapped event blob (once)
    event_id = store.blob(unwrapped_blob, t_ms, True, db)
    log.warning(f"[SYNC_STORE] Stored unwrapped blob: event_id={event_id[:20]}..., size={len(unwrapped_blob)}B")

    # Create recorded event for EACH peer who can decrypt
    # This ensures each peer gets their own view of when they recorded the event
    # The recorded.create() with return_dupes=True will handle deduplication
    # (if a recorded event for this event_id + recorded_by already exists, it returns the existing ID)
    log.info(f"unwrap_and_store: Creating recorded events for {len(recorded_by_peers)} peers: {[p[:20] for p in recorded_by_peers]}")
    recorded_ids = []
    for recorded_by in recorded_by_peers:
        recorded_id = recorded.create(event_id, recorded_by, t_ms, db, True)
        recorded_ids.append(recorded_id)
        log.info(f"unwrap_and_store: Created recorded event {recorded_id[:30]} for peer {recorded_by[:20]}")

    return recorded_ids


_receive_call_count = 0

def receive(batch_size: int, t_ms: int, db: Any) -> None:
    """Receive and process a batch of incoming transit blobs."""
    global _receive_call_count
    _receive_call_count += 1

    import logging
    log = logging.getLogger(__name__)

    if _receive_call_count > 1000:
        log.error(f"[SYNC_RECEIVE] CALL LIMIT EXCEEDED count={_receive_call_count} - possible infinite loop!")
        return

    transit_blobs = queues.incoming.drain(batch_size, db)
    log.warning(f"[SYNC_RECEIVE] batch_size={batch_size} drained={len(transit_blobs)}_blobs t_ms={t_ms} call_count={_receive_call_count}")

    # unwrap_and_store now returns a list of recorded_ids (one per peer who can decrypt)
    new_recorded_id_lists = []
    for i, blob in enumerate(transit_blobs):
        log.warning(f"[SYNC_RECEIVE] unwrapping blob {i+1}/{len(transit_blobs)} size={len(blob)}B")
        result = unwrap_and_store(blob, t_ms, db)
        new_recorded_id_lists.append(result)
        log.warning(f"[SYNC_RECEIVE] blob {i+1}/{len(transit_blobs)} unwrapped successfully, got {len(result)} recorded_ids")
    log.info(f"Unwrapped {len(new_recorded_id_lists)} blobs, got recorded_id lists: {new_recorded_id_lists}")

    # Flatten the list of lists to get all recorded_ids
    valid_recorded_ids = [id for id_list in new_recorded_id_lists for id in id_list]
    log.info(f"Valid recorded_ids to project: {valid_recorded_ids}")

    recorded.project_ids(valid_recorded_ids, db)

    # Check if any joiner just completed bootstrap (received inviter's events)
    # If so, create network_joined event to mark bootstrap success
    unsafedb = create_unsafe_db(db)
    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    for peer_row in local_peers:
        peer_id = peer_row['peer_id']

        # Check if this peer is a network creator - if so, skip bootstrap detection
        safedb = create_safe_db(db, recorded_by=peer_id)
        status = safedb.query_one(
            "SELECT created_network FROM bootstrap_status WHERE peer_id = ? AND recorded_by = ?",
            (peer_id, peer_id)
        )
        if status and status['created_network'] == 1:
            continue  # Network creator, not a joiner

        # Check if this peer has an invite (they're a joiner, not a creator)
        # Invites are stored as events with type='invite' in the recorded_by's valid_events
        invite_rows = safedb.query(
            "SELECT event_id FROM valid_events WHERE recorded_by = ?",
            (peer_id,)
        )

        inviter_peer_shared_id = None
        for invite_row in invite_rows:
            event_id = invite_row['event_id']
            blob = store.get(event_id, db)
            if blob:
                try:
                    event_data = crypto.parse_json(blob)
                    if event_data.get('type') == 'invite':
                        inviter_peer_shared_id = event_data.get('inviter_peer_shared_id')
                        break
                except:
                    pass

        if not inviter_peer_shared_id:
            continue  # Not a joiner, skip

        # Check if network_joined already exists for this peer
        existing_joined = False
        joined_events = safedb.query(
            "SELECT event_id FROM valid_events WHERE recorded_by = ?",
            (peer_id,)
        )
        for joined_row in joined_events:
            event_id = joined_row['event_id']
            blob = store.get(event_id, db)
            if blob:
                try:
                    event_data = crypto.parse_json(blob)
                    if event_data.get('type') == 'network_joined':
                        existing_joined = True
                        break
                except:
                    pass

        if existing_joined:
            continue  # Already created network_joined

        # Check if we've received ANY valid events from inviter (bootstrap successful)
        received_from_inviter = False
        for valid_row in safedb.query("SELECT event_id FROM valid_events WHERE recorded_by = ?", (peer_id,)):
            event_id = valid_row['event_id']
            blob = store.get(event_id, db)
            if blob:
                try:
                    event_data = crypto.parse_json(blob)
                    # Check if event was created by inviter (their peer_shared_id)
                    if event_data.get('created_by') == inviter_peer_shared_id:
                        received_from_inviter = True
                        break
                    # Also check for peer_id field in case of peer events
                    if event_data.get('peer_id') == inviter_peer_shared_id:
                        received_from_inviter = True
                        break
                except:
                    pass

        if received_from_inviter:
            # Bootstrap successful! Create network_joined event
            from events.identity import network_joined

            # Get peer_shared_id for this peer
            peer_shared_row = safedb.query_one(
                "SELECT peer_shared_id FROM peers_shared WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
                (peer_id, peer_id)
            )
            peer_shared_id = peer_shared_row['peer_shared_id'] if peer_shared_row else None

            if peer_shared_id:
                network_joined_id = network_joined.create(peer_id, peer_shared_id, inviter_peer_shared_id, t_ms, db)
                log.info(f"Bootstrap successful for {peer_id[:20]}... - created network_joined {network_joined_id[:20]}...")

                # Project the event immediately so bootstrap_complete gets set
                from events.transit import recorded as recorded_module
                # Create recorded event for network_joined
                recorded_id = recorded_module.create(network_joined_id, peer_id, t_ms, db, return_dupes=True)
                recorded_module.project_ids([recorded_id], db)

    # Event-driven unblocking now happens automatically in recorded.project()
    # via queues.blocked.notify_event_valid() - no need for scan-all loop

    db.commit()


def send_request_to_all(t_ms: int, db: Any) -> None:
    """All local peers send sync requests to all peers they've seen."""
    import logging
    log = logging.getLogger(__name__)

    # Query all local peers
    unsafedb = create_unsafe_db(db)
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")
    log.info(f"sync_all: found {len(local_peer_rows)} local peers")

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
            log.info(f"sync_all: skipping peer {peer_id_str[:30]} (no peer_shared_id)")
            continue  # Skip if we can't find peer_shared_id

        log.info(f"sync_all: peer_id={peer_id_str} peer_shared_id={peer_shared_id} sending_requests")
        # Send sync requests from this peer to all peers they've seen
        send_requests(peer_id, peer_shared_id, t_ms, db)


def send_requests(from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send sync requests to all peers this peer has seen."""
    import logging
    log = logging.getLogger(__name__)

    # Standardize encoding for logging
    if isinstance(from_peer_id, bytes):
        peer_id_str = crypto.b64encode(from_peer_id)
    else:
        peer_id_str = from_peer_id

    # Check my bootstrap status (subjective table, use safedb)
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    status = safedb.query_one(
        "SELECT created_network, joined_network FROM bootstrap_status WHERE peer_id = ? AND recorded_by = ?",
        (from_peer_id, from_peer_id)
    )

    created_network = status['created_network'] if status else 0
    joined_network = status['joined_network'] if status else 0
    my_bootstrap_complete = (created_network == 1 or joined_network == 1)

    log.info(f"send_requests: from_peer_id={peer_id_str[:20]}... created={created_network} joined={joined_network} my_bootstrap_complete={my_bootstrap_complete}")

    # Query all peer_shared events seen by this peer
    peer_shared_rows = safedb.query(
        "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
        (from_peer_id,)
    )

    log.info(f"send_requests: from_peer_id={peer_id_str[:20]}... found={len(peer_shared_rows)}_peers")

    for row in peer_shared_rows:
        ps_id = row['peer_shared_id']

        # Skip self (don't sync with yourself)
        if ps_id == from_peer_shared_id:
            log.info(f"send_requests: from_peer_id={peer_id_str[:20]}... skipping_self={ps_id[:20]}...")
            continue

        # Check per-relationship bootstrap: have we synced with this peer before?
        unsafedb = create_unsafe_db(db)
        relationship_state = unsafedb.query_one(
            "SELECT 1 FROM sync_state_ephemeral WHERE from_peer_id = ? AND to_peer_id = ?",
            (from_peer_id, ps_id)
        )
        relationship_bootstrap_complete = bool(relationship_state)

        log.warning(f"[BOOTSTRAP_CHECK] from={peer_id_str[:10]}... to={ps_id[:10]}... my_complete={my_bootstrap_complete} rel_complete={relationship_bootstrap_complete}")

        # Use bootstrap if EITHER:
        # 1. We haven't completed our own bootstrap (joined/created network), OR
        # 2. We haven't synced with this specific peer before (no sync_state)
        if not my_bootstrap_complete or not relationship_bootstrap_complete:
            # Bootstrap mode
            log.warning(f"[BOOTSTRAP_MODE] {peer_id_str[:20]}... -> {ps_id[:20]}... mode=bootstrap (my_complete={my_bootstrap_complete}, rel_complete={relationship_bootstrap_complete})")
            send_bootstrap_to_peer(ps_id, from_peer_id, from_peer_shared_id, t_ms, db)
        else:
            # Use normal bloom-filtered sync
            log.info(f"send_requests: {peer_id_str[:20]}... -> {ps_id[:20]}... mode=sync")
            send_request(ps_id, from_peer_id, from_peer_shared_id, t_ms, db)

    db.commit()


def send_bootstrap_to_peer(to_peer_shared_id: str, from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send ALL shareable events to a peer (bootstrap mode).

    Used when bootstrap_complete=0 for a peer relationship.
    Sends all shareable events without bloom filtering or window restrictions.

    Args:
        to_peer_shared_id: Recipient's peer_shared_id
        from_peer_id: Sender's local peer_id
        from_peer_shared_id: Sender's peer_shared_id
        t_ms: Current timestamp
        db: Database connection
    """
    import logging
    log = logging.getLogger(__name__)

    log.info(f"send_bootstrap_to_peer: from={from_peer_id[:20]}... to={to_peer_shared_id[:20]}... sending_all_shareable_events")

    # Get ALL shareable events for this peer (no window filtering)
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    all_shareable = safedb.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ? ORDER BY created_at ASC",
        (from_peer_id,)
    )

    log.info(f"send_bootstrap_to_peer: found {len(all_shareable)} shareable events to send")

    if not all_shareable:
        log.info(f"send_bootstrap_to_peer: no events to send, skipping")
        return

    # Get recipient's prekey for wrapping
    # Try to find prekey in transit_prekeys_shared table
    prekey_row = safedb.query_one(
        "SELECT transit_prekey_shared_id, transit_prekey_id, public_key FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ? ORDER BY created_at DESC LIMIT 1",
        (to_peer_shared_id, from_peer_id)
    )

    if not prekey_row:
        log.warning(f"send_bootstrap_to_peer: no prekey found for {to_peer_shared_id[:20]}..., cannot send")
        return

    # Build key dict for crypto.wrap() with transit_prekey_shared_id as hint
    transit_prekey_shared_id = prekey_row['transit_prekey_shared_id']
    recipient_prekey_public_key = prekey_row['public_key']
    recipient_key_dict = {
        'id': crypto.b64decode(transit_prekey_shared_id),
        'public_key': recipient_prekey_public_key,
        'type': 'asymmetric'
    }

    # Wrap and send each event
    for row in all_shareable:
        event_id = row['event_id']
        event_blob = store.get(event_id, db)
        if not event_blob:
            continue

        # Wrap with recipient's prekey (asymmetric encryption) including hint
        wrapped = crypto.wrap(event_blob, recipient_key_dict, db)
        queues.incoming.add(wrapped, t_ms, db)

    log.info(f"send_bootstrap_to_peer: sent {len(all_shareable)} events to {to_peer_shared_id[:20]}...")

    # Note: We do NOT mark bootstrap as complete here (sender-side marking is incorrect)
    # Bootstrap completion is determined by the receiver after successful validation
    # The joiner creates a network_joined event when they receive and validate inviter's events


def send_request(to_peer_shared_id: str, from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send bloom-based sync request to peer for specific window.

    Args:
        to_peer_shared_id: Recipient's peer_shared_id (public identity)
        from_peer_id: Sender's local peer_id
        from_peer_shared_id: Sender's peer_shared_id (public identity)
        t_ms: Timestamp
        db: Database connection
    """
    import logging
    log = logging.getLogger(__name__)

    log.warning(f"[SEND_REQUEST_ENTRY] from={from_peer_id[:20]}... to={to_peer_shared_id[:20]}...")

    # Get next window to sync
    window_id, w_param = get_next_window(from_peer_id, to_peer_shared_id, t_ms, db)
    log.warning(f"[SEND_REQUEST_WINDOW] from={from_peer_id[:20]}... window_id={window_id}, w_param={w_param}")
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
        log.warning(f"[SEND_REQUEST_EVENTS] from={from_peer_id[:10]}... window={window_id} range={window_min}-{window_max}")
        for evt in my_events_in_window:
            log.warning(f"[SEND_REQUEST_EVENTS]   - event={evt['event_id'][:20]}... storage_window={evt['window_id']}")

    # Build list of event_id bytes for bloom
    event_id_bytes_list = [crypto.b64decode(row['event_id']) for row in my_events_in_window]

    # Derive salt for this window (from requester's peer public key)
    requester_public_key = peer.get_public_key(from_peer_id, from_peer_id, db)
    salt = derive_salt(requester_public_key, window_id)

    # Create bloom filter of events requester HAS
    bloom_filter = create_bloom(event_id_bytes_list, salt)

    # Debug: Log bloom creation
    bits_set = bin(int.from_bytes(bloom_filter, 'big')).count('1')
    log.warning(f"[SEND_REQUEST_BLOOM] from={from_peer_id[:10]}... window={window_id} events_in_bloom={len(event_id_bytes_list)} bits_set={bits_set}/512")

    # Create a transit key for the response (owned by requester so they can decrypt response)
    response_transit_key_id = transit_key.create(from_peer_id, t_ms, db)
    response_transit_key = transit_key.get_key(response_transit_key_id, from_peer_id, db)

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

    # Store as signed plaintext
    canonical = crypto.canonicalize_json(signed_request)

    # Wrap with recipient's prekey for transit only
    to_key = transit_prekey.get_transit_prekey_for_peer(to_peer_shared_id, from_peer_id, db)
    if to_key:
        import logging
        log.info(f"send_request: wrapping with hint={crypto.b64encode(to_key['id'])[:30]}...")
    else:
        import logging
        log.warning(f"send_request: NO PREKEY FOUND for {to_peer_shared_id[:20]}...")
    request_blob = crypto.wrap(canonical, to_key, db)

    # simulate sending - add to incoming queue
    queues.incoming.add(request_blob, t_ms, db)

    # Mark window as synced (optimistically - in production might wait for response)
    mark_window_synced(from_peer_id, to_peer_shared_id, window_id, t_ms, db)


def project_ephemeral(sync_event_id: str, sync_data: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Handle sync request ephemerally (without storing in event log).

    This is called directly from unwrap_and_store() for sync events.
    """
    _project_sync_event(sync_event_id, sync_data, recorded_by, recorded_at, db)


def project(sync_event_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Handle sync request by sending bloom-filtered response (for stored sync events)."""
    import logging
    log = logging.getLogger(__name__)
    log.warning(f"[SYNC_PROJECT] sync_id={sync_event_id[:20]}... recorded_by={recorded_by[:10]}...")

    # Get the sync event data from store
    sync_blob = store.get(sync_event_id, db)
    if not sync_blob:
        log.info(f"sync blob not found in store")
        return

    # Parse sync request data (signed plaintext)
    sync_data = crypto.parse_json(sync_blob)

    _project_sync_event(sync_event_id, sync_data, recorded_by, recorded_at, db)


def _project_sync_event(sync_event_id: str, sync_data: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Internal function to handle sync request logic (shared between ephemeral and stored)."""
    import logging
    log = logging.getLogger(__name__)
    log.warning(f"[SYNC_PROJECT] sync_id={sync_event_id[:20]}... recorded_by={recorded_by[:10]}...")

    # Verify signature using requester's peer_shared public key
    requester_peer_shared_id = sync_data.get('peer_shared_id')
    if not requester_peer_shared_id:
        log.warning(f"sync.project() missing peer_shared_id in sync request")
        return

    try:
        from events.identity import peer_shared
        requester_public_key = peer_shared.get_public_key(requester_peer_shared_id, recorded_by, db)
        if not crypto.verify_event(sync_data, requester_public_key):
            log.warning(f"sync.project() signature verification failed for sync request from {requester_peer_shared_id[:20]}...")
            return
    except ValueError as e:
        # peer_shared not projected yet - return without processing
        log.debug(f"sync.project() peer_shared not available yet for signature verification: {e}")
        return

    # Extract requester info
    requester_peer_id = sync_data.get('peer_id')
    transit_key_encoded = sync_data.get('transit_key')
    window_id = sync_data.get('window_id')
    window_min = sync_data.get('window_min')
    window_max = sync_data.get('window_max')
    bloom_b64 = sync_data.get('bloom')

    log.info(f"sync.project() processing sync request: window_id={window_id}, window_range={window_min}-{window_max}")
    log.info(f"sync.project() requester={requester_peer_id[:20] if requester_peer_id else None}..., recorded_by={recorded_by[:20]}..., requester==recorded_by: {requester_peer_id == recorded_by if requester_peer_id else False}")

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
        log.warning(f"[SYNC_PROJECT] result=REJECTED requester={requester_peer_shared_id[:20]}... not_recognized_by={recorded_by[:10]}...")
        return

    log.warning(f"[SYNC_PROJECT] result=ACCEPTED requester={requester_peer_shared_id[:20]}... recognized_by={recorded_by[:10]}... window={window_id}")

    # Check if we've synced with this peer before
    unsafedb = create_unsafe_db(db)
    sync_state_exists = unsafedb.query_one(
        "SELECT 1 FROM sync_state_ephemeral WHERE from_peer_id = ? AND to_peer_id = ?",
        (recorded_by, requester_peer_shared_id)
    )

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
    from events.identity import peer_shared
    requester_public_key = peer_shared.get_public_key(requester_peer_shared_id, recorded_by, db)

    if not sync_state_exists:
        # First sync with this peer - send bloom-filtered events from ALL windows
        log.warning(f"[SYNC_PROJECT] first_sync_with_requester={requester_peer_shared_id[:20]}... sending_bootstrap_response")
        send_bootstrap_response(recorded_by, transit_key_dict, bloom_filter, requester_public_key, window_id, recorded_at, db)
        # Mark that we've now bootstrapped with this peer
        update_sync_state(recorded_by, requester_peer_shared_id, 0, 1, 0, recorded_at, db)
    else:
        # Normal bloom-filtered sync (single window)
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


def send_bootstrap_response(from_peer_id: str, transit_key_dict: dict[str, Any], bloom_filter: bytes,
                            requester_public_key: bytes, window_id: int, t_ms: int, db: Any) -> None:
    """Send shareable events filtered by bloom (from ALL windows, not just requested window).

    This is used for first-time sync with a new peer - we check all windows and send
    any events the requester doesn't have (based on their bloom filter).

    Args:
        from_peer_id: Responder's peer_id (which peer is sending the response)
        transit_key_dict: Transit key dict from the sync request (for wrapping responses)
        bloom_filter: Bloom filter from sync request showing what requester has
        requester_public_key: For deriving bloom salt
        window_id: Window ID from sync request (for salt derivation)
        t_ms: Current timestamp
        db: Database connection
    """
    import logging
    log = logging.getLogger(__name__)

    log.warning(f"[BOOTSTRAP_RESPONSE] from={from_peer_id[:10]}... checking_all_windows")

    # Get ALL shareable events for this peer (from all windows)
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    all_shareable = safedb.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ? ORDER BY created_at ASC",
        (from_peer_id,)
    )

    log.warning(f"[BOOTSTRAP_RESPONSE] found={len(all_shareable)}_shareable_events from={from_peer_id[:10]}...")

    # Derive salt for bloom checking
    salt = derive_salt(requester_public_key, window_id)

    # Filter using bloom and send events requester doesn't have
    events_sent = 0
    for row in all_shareable:
        event_id = row['event_id']
        event_id_bytes = crypto.b64decode(event_id)

        # Check if event is in requester's bloom
        in_bloom = check_bloom(event_id_bytes, bloom_filter, salt)
        if in_bloom:
            continue  # Requester already has this event

        # Send this event
        try:
            event_blob = safedb.get_shareable_blob(event_id)
        except Exception as e:
            log.warning(f"send_bootstrap_response: failed to get shareable blob for {event_id[:20]}...: {e}")
            continue

        # Log event type
        try:
            event_data = crypto.parse_json(event_blob)
            event_type = event_data.get('type', 'unknown')
            log.warning(f"[BOOTSTRAP_RESPONSE] sending {event_type} event {event_id[:20]}...")
        except:
            log.warning(f"[BOOTSTRAP_RESPONSE] sending encrypted event {event_id[:20]}...")

        # Wrap with transit key from sync request
        wrapped = crypto.wrap(event_blob, transit_key_dict, db)

        # Send
        queues.incoming.add(wrapped, t_ms, db)
        events_sent += 1

    log.warning(f"[BOOTSTRAP_RESPONSE] sent={events_sent}_events (filtered_from_{len(all_shareable)}_total)")


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

    log.warning(f"[SYNC_RESPONSE] from={from_peer_id[:10]}... to={to_peer_id[:10]}... window={window_id} range={window_min}-{window_max}")

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
    log.warning(f"[SYNC_RESPONSE] found={len(shareable_rows)}_shareable_events from={from_peer_id[:10]}...")
    for row in shareable_rows:
        log.warning(f"[SYNC_RESPONSE]   candidate event={row['event_id'][:20]}...")

    # Derive salt for bloom checking (same salt requester used)
    salt = derive_salt(requester_public_key, window_id)

    # Debug: Log bloom filter stats
    bits_set = bin(int.from_bytes(bloom_filter, 'big')).count('1')
    log.warning(f"[SYNC_RESPONSE] bloom_filter_bits_set={bits_set}/512 bloom_hex={bloom_filter.hex()[:40]}...")

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
            log.warning(f"[SYNC_RESPONSE] will_send event_id={event_id_str[:20]}... (not_in_bloom)")
        else:
            log.warning(f"[SYNC_RESPONSE] skipping event_id={event_id_str[:20]}... (in_bloom)")

    log.warning(f"[SYNC_RESPONSE] sending={len(events_to_send)}_events to={to_peer_id[:10]}...")

    if len(events_to_send) == 0 and len(shareable_rows) > 0:
        log.warning(f"[SYNC_RESPONSE] WARNING: All {len(shareable_rows)} events were filtered by bloom! This suggests a bloom filter bug.")

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
        wrapped_blob = crypto.wrap(event_blob, transit_key_dict, db)
        queues.incoming.add(wrapped_blob, t_ms, db)
