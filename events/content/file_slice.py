"""File slice event type - encrypted 450-byte chunks of files.

Slices are NOT group-wrapped (access control via file descriptor event).
Slices are NOT signed (root_hash detects tampering).
Slices ARE transit-wrapped during sync (for routing).
"""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(file_id: str, slice_number: int, nonce: bytes, ciphertext: bytes,
           poly_tag: bytes, peer_id: str, created_by: str, t_ms: int,
           db: Any) -> str:
    """Create a file_slice event (encrypted chunk of file).

    IMPORTANT: Slices are NOT group-wrapped. Access control happens via file descriptor.

    Args:
        file_id: ID of the file this slice belongs to
        slice_number: Index of this slice (0-based)
        nonce: 24-byte nonce used for encryption
        ciphertext: Encrypted bytes (max 450 bytes)
        poly_tag: 16-byte AEAD authentication tag
        peer_id: Local peer creating this event
        created_by: Shareable peer_shared_id (for sync tracking)
        t_ms: Timestamp
        db: Database connection

    Returns:
        slice_event_id (BLAKE2b-128 of event)
    """
    log.info(f"file_slice.create() file_id={file_id}, slice_number={slice_number}, "
             f"ciphertext_size={len(ciphertext)}B")

    # Build event structure (NO signatures, NO wrapping)
    event_data = {
        'type': 'file_slice',
        'file_id': file_id,
        'slice_number': slice_number,
        'nonce': crypto.b64encode(nonce),
        'ciphertext': crypto.b64encode(ciphertext),
        'poly_tag': crypto.b64encode(poly_tag),
        'created_by': created_by,  # Shareable peer identity
        'created_at': t_ms
    }

    # Canonicalize (no signing, no encryption)
    canonical = crypto.canonicalize_json(event_data)

    # Store as plain event (no wrapping at all)
    slice_event_id = store.event(canonical, peer_id, t_ms, db)

    log.info(f"file_slice.create() created slice_event_id={slice_event_id[:20]}...")
    return slice_event_id


def project(event_id: str, event_data: dict[str, Any], recorded_by: str,
            recorded_at: int, db: Any) -> None:
    """Project file_slice event into file_slices table.

    Args:
        event_id: Event ID
        event_data: Decrypted/unwrapped event data
        recorded_by: Peer who recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection
    """
    log.debug(f"file_slice.project() event_id={event_id[:20]}..., recorded_by={recorded_by[:20]}...")

    file_id = event_data.get('file_id')
    slice_number = event_data.get('slice_number')
    nonce_b64 = event_data.get('nonce')
    ciphertext_b64 = event_data.get('ciphertext')
    poly_tag_b64 = event_data.get('poly_tag')

    if not all([file_id, slice_number is not None, nonce_b64, ciphertext_b64, poly_tag_b64]):
        log.warning(f"file_slice.project() missing fields in event_data: {list(event_data.keys())}")
        return

    # Decode from base64
    nonce = crypto.b64decode(nonce_b64)
    ciphertext = crypto.b64decode(ciphertext_b64)
    poly_tag = crypto.b64decode(poly_tag_b64)

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Insert or ignore
    safedb.execute(
        """INSERT OR IGNORE INTO file_slices
           (file_id, slice_number, nonce, ciphertext, poly_tag, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (file_id, slice_number, nonce, ciphertext, poly_tag, recorded_by, recorded_at)
    )

    log.debug(f"file_slice.project() projected slice {file_id[:20]}.../{slice_number}")
