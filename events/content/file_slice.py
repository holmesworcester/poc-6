"""File slice event type - encrypted 450-byte chunks of files.

Slices are NOT group-wrapped (access control via file descriptor event).
Slices are NOT signed (root_hash detects tampering).
Slices ARE transit-wrapped during sync (for routing).

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    create(...) -> str
    project_event(...) -> str | None
"""
from typing import Any, TypedDict
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class FileSliceEventData(TypedDict):
    type: str
    file_id: str
    slice_number: int
    nonce: str  # base64
    ciphertext: str  # base64
    poly_tag: str  # base64
    signed_by: str
    created_at: int


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result.

    Outputs file_slices row and event_dependencies row (slice depends on file).
    """
    from projection import ProjectorResult

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    file_id = event_data.get("file_id")
    slice_number = event_data.get("slice_number")
    nonce_b64 = event_data.get("nonce")
    ciphertext_b64 = event_data.get("ciphertext")
    poly_tag_b64 = event_data.get("poly_tag")

    if not all([file_id, slice_number is not None, nonce_b64, ciphertext_b64, poly_tag_b64]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Decode from base64
    nonce = crypto.b64decode(nonce_b64)
    ciphertext = crypto.b64decode(ciphertext_b64)
    poly_tag = crypto.b64decode(poly_tag_b64)

    # Output: file_slices row
    slice_row = {
        "file_id": file_id,
        "slice_number": slice_number,
        "nonce": nonce,
        "ciphertext": ciphertext,
        "poly_tag": poly_tag,
        "event_id": event_id,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    # Output: event_dependencies row (slice depends on file)
    dep_row = {
        "child_event_id": event_id,
        "parent_event_id": file_id,
        "recorded_by": recorded_by,
        "dependency_type": "file",
    }

    return ProjectorResult(
        valid=True,
        tables={
            "file_slices": [slice_row],
            "event_dependencies": [dep_row],
        },
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    file_id: str = "file_123",
    slice_number: int = 0,
    nonce: str = "AAAAAAAAAAAAAAAAAAAAAA==",  # 16 bytes base64
    ciphertext: str = "SGVsbG8gV29ybGQ=",  # "Hello World" base64
    poly_tag: str = "AAAAAAAAAAAAAAAAAAAAAA==",  # 16 bytes base64
    signed_by: str = "peer_shared_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "file_slice",
        "file_id": file_id,
        "slice_number": slice_number,
        "nonce": nonce,
        "ciphertext": ciphertext,
        "poly_tag": poly_tag,
        "signed_by": signed_by,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "slice_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }


# ============================================================================
# API FUNCTIONS
# ============================================================================

# Disable per-slice logging during batch operations
_batch_mode = False


def create(file_id: str, slice_number: int, nonce: bytes, ciphertext: bytes,
           poly_tag: bytes, peer_id: str, signed_by: str, t_ms: int,
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
        signed_by: Shareable peer_shared_id (for sync tracking)
        t_ms: Timestamp
        db: Database connection

    Returns:
        slice_event_id (BLAKE2b-128 of event)
    """
    if not _batch_mode:
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
        'signed_by': signed_by,  # Shareable peer identity
        'created_at': t_ms
    }

    # Canonicalize (no signing, no encryption)
    canonical = crypto.canonicalize_json(event_data)

    # Store as plain event (no wrapping at all)
    slice_event_id = store.event(canonical, peer_id, t_ms, db)

    if not _batch_mode:
        log.info(f"file_slice.create() created slice_event_id={slice_event_id[:20]}...")
    return slice_event_id


def project_event(event_id: str, event_data: dict[str, Any], recorded_by: str,
            recorded_at: int, db: Any) -> None:
    """Project file_slice event using pure projector.

    Note: file_slice receives event_data directly from dispatch (already parsed),
    unlike most projectors that use resolve() to unwrap.

    Args:
        event_id: Event ID
        event_data: Decrypted/unwrapped event data
        recorded_by: Peer who recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection
    """
    from projectors import apply_result
    from projectors import file_slice as fs_projector

    log.debug(f"file_slice.project() event_id={event_id[:20]}..., recorded_by={recorded_by[:20]}...")

    # Build input dict directly (event_data already parsed by caller)
    input_dict = {
        "event_id": event_id,
        "event_data": event_data,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }

    # Call pure projector
    result = fs_projector.project(input_dict)

    if not result.valid:
        log.warning(f"file_slice.project() rejected: {result.reason}")
        return

    # Apply result: insert into tables
    apply_result(result, recorded_by, recorded_at, db)

    file_id = event_data.get('file_id', '')
    slice_number = event_data.get('slice_number', 0)
    log.debug(f"file_slice.project() projected slice {file_id[:20]}.../{slice_number}")


def batch_create_slices(file_id: str, slices_data: list[tuple], peer_id: str,
                        signed_by: str, t_ms: int, db: Any) -> int:
    """Efficiently create many file slices in batch mode.

    Uses optimized batch storage without immediate projection for massive performance gains.

    Args:
        file_id: ID of the file these slices belong to
        slices_data: List of (slice_number, nonce, ciphertext, poly_tag) tuples
        peer_id: Local peer creating these events
        signed_by: Shareable peer_shared_id
        t_ms: Timestamp
        db: Database connection

    Returns:
        Number of slices created
    """
    global _batch_mode
    import store
    from db import create_safe_db

    if not slices_data:
        return 0

    # Build all event blobs upfront
    event_blobs = []
    for slice_number, slice_nonce, ciphertext, poly_tag in slices_data:
        event_data = {
            'type': 'file_slice',
            'file_id': file_id,
            'slice_number': slice_number,
            'nonce': crypto.b64encode(slice_nonce),
            'ciphertext': crypto.b64encode(ciphertext),
            'poly_tag': crypto.b64encode(poly_tag),
            'signed_by': signed_by,
            'created_at': t_ms
        }
        canonical = crypto.canonicalize_json(event_data)
        event_blobs.append(canonical)

    # Store all events in bulk AND project them directly (skipping recorded.project overhead)
    old_store_batch = store._batch_mode
    store._batch_mode = True

    try:
        event_ids = store.batch_store_events(event_blobs, peer_id, t_ms, db)

        # Project all slices efficiently (bulk insert into file_slices table)
        safedb = create_safe_db(db, recorded_by=peer_id)

        # Bulk insert all slices at once for maximum efficiency
        for event_id, (slice_number, slice_nonce, ciphertext, poly_tag) in zip(event_ids, slices_data):
            # Insert into file_slices table
            safedb.execute(
                """INSERT OR IGNORE INTO file_slices
                   (file_id, slice_number, nonce, ciphertext, poly_tag, event_id, recorded_by, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_id, slice_number, slice_nonce, ciphertext, poly_tag, event_id, peer_id, t_ms)
            )

            # Record dependency
            safedb.execute(
                """INSERT OR IGNORE INTO event_dependencies
                   (child_event_id, parent_event_id, recorded_by, dependency_type)
                   VALUES (?, ?, ?, ?)""",
                (event_id, file_id, peer_id, 'file')
            )

        # Mark slices as shareable (sync via regular bloom filter sync)
        # File slices are regular shareable events - they sync like any other event type
        # Access control is enforced at message_attachment level (group-encrypted)
        from events.network import sync
        for event_id in event_ids:
            sync.add_shareable_event(
                event_id=event_id,
                can_share_peer_id=peer_id,
                created_at=None,  # Sync uses recorded_at for ordering
                recorded_at=t_ms,
                db=db
            )

        log.info(f"file_slice.batch_create_slices() created {len(event_ids)} slices for file {file_id[:20]}...")
        return len(event_ids)

    finally:
        store._batch_mode = old_store_batch
