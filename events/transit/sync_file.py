"""Prioritized sync for file slices (sync_file events).

Unlike regular sync which syncs all events opportunistically, sync_file provides
prioritized, on-demand synchronization of file slices for specific files.
File descriptors (encryption metadata) stay in message_attachment events and sync
via regular sync.
"""
from typing import Any
import logging
import hashlib
import math
import crypto
import store
from events.identity import peer_shared
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)

# Bloom filter parameters (reuse from regular sync)
BLOOM_SIZE_BITS = 512
BLOOM_SIZE_BYTES = 64
K_HASHES = 5

# File-specific window calculation (from ideal protocol)
# For blobs: W = max(1, ceil(total_slices / 100)) up to 4096 (w=12)
# This ensures ~50-100 slices per window for low false-positive rate
DEFAULT_BLOB_SLICES_PER_WINDOW = 100
MAX_W = 12  # Maximum 2^12 = 4096 windows


def compute_file_w_param(blob_bytes: int) -> int:
    """Compute file-specific window parameter from ideal protocol.

    For files: W = max(1, ceil(total_slices / 100)) up to 4096 (w=12)
    where total_slices = ceil(blob_bytes / 450)

    This ensures optimal ~50-100 slices per window for low FPR.

    Args:
        blob_bytes: Total file size in bytes

    Returns:
        w parameter (number of bits for windows)
    """
    SLICE_SIZE = 450
    total_slices = math.ceil(blob_bytes / SLICE_SIZE)

    # Calculate optimal number of windows (target 100 slices per window)
    target_windows = max(1, math.ceil(total_slices / DEFAULT_BLOB_SLICES_PER_WINDOW))

    # Convert to w bits: 2^w >= target_windows
    if target_windows == 1:
        w = 0
    else:
        w = max(1, math.ceil(math.log2(target_windows)))

    # Clamp to maximum
    w = min(w, MAX_W)

    return w


def compute_window_id_for_file(event_id: bytes, w: int) -> int:
    """Compute window ID for file slices (same as regular sync).

    window_id = high-order w bits of BLAKE2b-256(event_id)
    """
    h = hashlib.blake2b(event_id, digest_size=32)
    hash_int = int.from_bytes(h.digest(), byteorder='big')
    return hash_int >> (256 - w)


def derive_salt(peer_pk: bytes, window_id: int) -> bytes:
    """Derive 16-byte bloom salt: BLAKE2b-128(peer_pk || window_id)."""
    window_id_bytes = window_id.to_bytes(4, byteorder='big')
    h = hashlib.blake2b(peer_pk + window_id_bytes, digest_size=16)
    return h.digest()


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


def request_file_sync(file_id: str, peer_id: str, priority: int, ttl_ms: int, t_ms: int, db: Any) -> None:
    """Mark file for active syncing (user action).

    Args:
        file_id: File to sync
        peer_id: Local peer doing the syncing
        priority: Urgency (1-10, higher = more urgent)
        ttl_ms: When to stop trying (0 = forever)
        t_ms: Current timestamp
        db: Database connection
    """
    log.info(f"sync_file.request_file_sync() file_id={file_id[:20]}..., "
             f"peer_id={peer_id[:20]}..., priority={priority}, ttl_ms={ttl_ms}")

    safedb = create_safe_db(db, recorded_by=peer_id)
    safedb.execute(
        """INSERT OR REPLACE INTO file_sync_wanted
           (file_id, peer_id, priority, ttl_ms, requested_at)
           VALUES (?, ?, ?, ?, ?)""",
        (file_id, peer_id, priority, ttl_ms, t_ms)
    )


def cancel_file_sync(file_id: str, peer_id: str, db: Any) -> None:
    """Cancel active syncing for a file.

    Args:
        file_id: File to stop syncing
        peer_id: Local peer
        db: Database connection
    """
    log.info(f"sync_file.cancel_file_sync() file_id={file_id[:20]}..., peer_id={peer_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=peer_id)
    safedb.execute(
        "DELETE FROM file_sync_wanted WHERE file_id = ? AND peer_id = ?",
        (file_id, peer_id)
    )


def is_file_complete(file_id: str, peer_id: str, db: Any) -> bool:
    """Check if all slices for a file have been received.

    Args:
        file_id: File to check
        peer_id: Local peer
        db: Database connection

    Returns:
        True if all slices received, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get attachment to find total_slices
    attachment_row = safedb.query_one(
        "SELECT total_slices FROM message_attachments WHERE file_id = ? AND recorded_by = ? LIMIT 1",
        (file_id, peer_id)
    )
    if not attachment_row:
        return False

    total_slices = attachment_row['total_slices']

    # Count received slices
    slices_row = safedb.query_one(
        "SELECT COUNT(*) as count FROM file_slices WHERE file_id = ? AND recorded_by = ?",
        (file_id, peer_id)
    )
    received_slices = slices_row['count'] if slices_row else 0

    return received_slices >= total_slices


def get_file_sync_state(file_id: str, from_peer_id: str, to_peer_id: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Get file sync state for peer pair.

    Returns:
        {
            'last_window': last window synced (-1 = not started),
            'w_param': window parameter bits,
            'slices_received': number of slices received,
            'total_slices': total slices expected
        }
    """
    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one(
        "SELECT last_window, w_param, slices_received, total_slices FROM file_sync_state_ephemeral "
        "WHERE file_id = ? AND from_peer_id = ? AND to_peer_id = ?",
        (file_id, from_peer_id, to_peer_id)
    )

    if row:
        return {
            'last_window': row['last_window'],
            'w_param': row['w_param'],
            'slices_received': row['slices_received'],
            'total_slices': row['total_slices']
        }

    # Initialize state: query file metadata to get w_param and total_slices
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    attachment_row = safedb.query_one(
        "SELECT blob_bytes, total_slices FROM message_attachments WHERE file_id = ? AND recorded_by = ? LIMIT 1",
        (file_id, from_peer_id)
    )

    if not attachment_row:
        log.warning(f"sync_file.get_file_sync_state() file not found: {file_id[:20]}...")
        return {
            'last_window': -1,
            'w_param': 0,
            'slices_received': 0,
            'total_slices': 0
        }

    blob_bytes = attachment_row['blob_bytes']
    total_slices = attachment_row['total_slices']
    w_param = compute_file_w_param(blob_bytes)

    return {
        'last_window': -1,
        'w_param': w_param,
        'slices_received': 0,
        'total_slices': total_slices
    }


def update_file_sync_state(file_id: str, from_peer_id: str, to_peer_id: str,
                           last_window: int, w_param: int, slices_received: int,
                           total_slices: int, t_ms: int, db: Any) -> None:
    """Update file sync state after window synced.

    Args:
        file_id: File being synced
        from_peer_id: Local peer syncing
        to_peer_id: Remote peer serving
        last_window: Last window synced
        w_param: Window parameter bits
        slices_received: Number of slices received so far
        total_slices: Total slices expected
        t_ms: Current timestamp
        db: Database connection
    """
    unsafedb = create_unsafe_db(db)
    unsafedb.execute(
        """INSERT INTO file_sync_state_ephemeral
           (file_id, from_peer_id, to_peer_id, last_window, w_param, slices_received, total_slices, started_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (file_id, from_peer_id, to_peer_id)
           DO UPDATE SET
               last_window = excluded.last_window,
               w_param = excluded.w_param,
               slices_received = excluded.slices_received,
               updated_at = excluded.updated_at""",
        (file_id, from_peer_id, to_peer_id, last_window, w_param, slices_received, total_slices, t_ms, t_ms)
    )


def send_request(file_id: str, to_peer: str, from_peer_id: str, t_ms: int, db: Any) -> None:
    """Send sync_file request for specific file.

    Args:
        file_id: File to sync
        to_peer: Remote peer to request from (peer_shared_id)
        from_peer_id: Local peer making request
        t_ms: Current timestamp
        db: Database connection
    """
    log.info(f"sync_file.send_request() file_id={file_id[:20]}..., "
             f"from={from_peer_id[:20]}..., to={to_peer[:20]}...")

    # Get current sync state for this file/peer pair
    state = get_file_sync_state(file_id, from_peer_id, to_peer, t_ms, db)

    if state['total_slices'] == 0:
        log.warning(f"sync_file.send_request() file not found or no slices: {file_id[:20]}...")
        return

    # Get next window
    w_param = state['w_param']
    total_windows = 2 ** w_param
    next_window = (state['last_window'] + 1) % total_windows

    # Get slices we have in this window
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    our_slices = safedb.query(
        """SELECT fs.slice_number, fs.file_id FROM file_slices fs
           WHERE fs.file_id = ? AND fs.recorded_by = ?
           ORDER BY fs.slice_number ASC""",
        (file_id, from_peer_id)
    )

    our_slice_numbers = {row['slice_number'] for row in our_slices}
    log.debug(f"sync_file.send_request() file_id={file_id[:20]}... "
              f"w_param={w_param}, next_window={next_window}, we_have={len(our_slice_numbers)}/{state['total_slices']} slices")

    # Create bloom of slices we HAVE (to tell responder which ones to skip)
    # For file slices, create synthetic event IDs based on file_id + slice_number
    our_event_ids = []
    for slice_num in our_slice_numbers:
        # Create deterministic event ID: BLAKE2b-128(file_id || slice_number)
        event_id_bytes = crypto.b64decode(file_id)
        slice_bytes = slice_num.to_bytes(4, byteorder='big')
        syn_id = crypto.hash(event_id_bytes + slice_bytes, 16)
        our_event_ids.append(syn_id)

    # Derive salt from our peer_shared public key
    from events.identity import peer_shared as peer_shared_module
    try:
        # Get our peer_shared_id from local peers table
        unsafedb = create_unsafe_db(db)
        our_peer_shared_row = unsafedb.query_one(
            "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ? LIMIT 1",
            (from_peer_id,)
        )
        if not our_peer_shared_row:
            log.warning(f"sync_file.send_request() peer_shared not found for {from_peer_id[:20]}...")
            return

        our_peer_shared_id = our_peer_shared_row['peer_shared_id']
        our_public_key = peer_shared_module.get_public_key(our_peer_shared_id, from_peer_id, db)
    except Exception as e:
        log.warning(f"sync_file.send_request() failed to get peer_shared public key: {e}")
        return

    salt = derive_salt(our_public_key, next_window)
    bloom_filter = create_bloom(our_event_ids, salt)

    log.debug(f"sync_file.send_request() bloom created with {len(our_event_ids)} slices, "
              f"bits_set={bin(int.from_bytes(bloom_filter, 'big')).count('1')}/512")

    # Create sync_file event
    event_data = {
        'type': 'sync_file',
        'file_id': file_id,
        'requester_peer_id': from_peer_id,
        'window_id': next_window,
        'w_param': w_param,
        'bloom': crypto.b64encode(bloom_filter),
        'created_at': t_ms
    }

    # Sign with our private key
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Canonicalize
    canonical = crypto.canonicalize_json(signed_event)

    # Store as plain event (not wrapped, sync_file is ephemeral)
    sync_file_event_id = store.event(canonical, from_peer_id, t_ms, db)

    log.info(f"sync_file.send_request() created sync_file event {sync_file_event_id[:20]}...")


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any, sync_file_data: dict[str, Any] | None = None) -> None:
    """Project sync_file event (handle request, send response with slices).

    Args:
        event_id: sync_file event ID
        recorded_by: Peer who recorded this event
        recorded_at: When recorded
        db: Database connection
        sync_file_data: Optional parsed event data (for ephemeral handling)
    """
    if sync_file_data is None:
        # Load from store
        sync_file_blob = store.get(event_id, db)
        if not sync_file_blob:
            log.warning(f"sync_file.project() blob not found: {event_id[:20]}...")
            return
        sync_file_data = crypto.parse_json(sync_file_blob)

    log.info(f"sync_file.project() event_id={event_id[:20]}..., recorded_by={recorded_by[:20]}...")

    file_id = sync_file_data.get('file_id')
    requester_peer_id = sync_file_data.get('requester_peer_id')
    window_id = sync_file_data.get('window_id')
    w_param = sync_file_data.get('w_param')
    bloom_b64 = sync_file_data.get('bloom')

    if not all([file_id, requester_peer_id, window_id is not None, w_param is not None, bloom_b64]):
        log.warning(f"sync_file.project() missing required fields")
        return

    # Decode bloom
    bloom_filter = crypto.b64decode(bloom_b64)

    # Get responder's slices for this file in this window
    safedb = create_safe_db(db, recorded_by=recorded_by)
    responder_slices = safedb.query(
        """SELECT slice_number FROM file_slices
           WHERE file_id = ? AND recorded_by = ?
           ORDER BY slice_number ASC""",
        (file_id, recorded_by)
    )

    log.debug(f"sync_file.project() responder has {len(responder_slices)} slices for {file_id[:20]}...")

    # Get requester's public key for salt derivation
    try:
        from events.identity import peer_shared as peer_shared_module
        requester_public_key = peer_shared_module.get_public_key(requester_peer_id, recorded_by, db)
    except Exception as e:
        log.warning(f"sync_file.project() failed to get requester public key: {e}")
        return

    salt = derive_salt(requester_public_key, window_id)

    # Filter: send slices that FAIL bloom check (requester doesn't have them)
    slices_to_send = []
    for row in responder_slices:
        slice_num = row['slice_number']

        # Synthetic event ID for this slice
        event_id_bytes = crypto.b64decode(file_id)
        slice_bytes = slice_num.to_bytes(4, byteorder='big')
        syn_id = crypto.hash(event_id_bytes + slice_bytes, 16)

        in_bloom = check_bloom(syn_id, bloom_filter, salt)

        if not in_bloom:
            slices_to_send.append(slice_num)
            log.debug(f"sync_file.project() will send slice {slice_num}")

    log.info(f"sync_file.project() sending {len(slices_to_send)} slices to requester")

    # TODO: Implement actual slice sending
    # This requires:
    # 1. Getting slice event IDs from file_slices table (need to add event_id column)
    # 2. Wrapping slice blobs with requester's transit prekey
    # 3. Adding wrapped slices to incoming queue
    # 4. Updating file_sync_state with progress
    #
    # For now, just log that slices would be sent
    log.debug(f"sync_file.project() would send slices: {slices_to_send}")
