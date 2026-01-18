"""File slice event type - encrypted 450-byte chunks of files.

Slices are NOT group-wrapped (access control via message_attachment event).
Slices are NOT signed - integrity is verified via root_hash in message_attachment.
Slices ARE transit-wrapped during sync (for routing).

Security model:
- message_attachment events are group-encrypted and signed
- message_attachment contains root_hash computed from all slice ciphertexts
- When retrieving files, root_hash is verified against actual slice data
- This chain: signed message_attachment → root_hash → file_slices
  provides integrity without per-slice signatures
"""

# Registry metadata
EVENT_TYPE = 'file_slice'
SHAREABLE = True  # File slices sync to message recipients
PROJECTION_TABLE = None  # No created_at lookup needed

from typing import Any
import logging
from core import crypto
from core import store
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# v2 event specification - unsigned, not encrypted
# Integrity verified via root_hash in message_attachment
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,  # Not signed - integrity via root_hash chain
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for file_slice events."""
    event_data = ctx.event_data

    file_id = event_data.get('file_id')
    slice_number = event_data.get('slice_number')
    nonce_b64 = event_data.get('nonce')
    ciphertext_b64 = event_data.get('ciphertext')
    poly_tag_b64 = event_data.get('poly_tag')

    if not all([file_id, slice_number is not None, nonce_b64, ciphertext_b64, poly_tag_b64]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Decode from base64
    nonce = crypto.b64decode(nonce_b64)
    ciphertext = crypto.b64decode(ciphertext_b64)
    poly_tag = crypto.b64decode(poly_tag_b64)

    writes = (
        WriteOp(
            op='insert',
            table='file_slices',
            values={
                'file_id': file_id,
                'slice_number': slice_number,
                'nonce': nonce,
                'ciphertext': ciphertext,
                'poly_tag': poly_tag,
                'event_id': ctx.event_id,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
        WriteOp(
            op='insert',
            table='event_dependencies',
            values={
                'child_event_id': ctx.event_id,
                'parent_event_id': file_id,
                'recorded_by': ctx.recorded_by,
                'dependency_type': 'file',
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)

# Disable per-slice logging during batch operations
_batch_mode = False


def create(file_id: str, slice_number: int, nonce: bytes, ciphertext: bytes,
           poly_tag: bytes, peer_id: str, t_ms: int,
           db: Any) -> str:
    """Create a file_slice event (encrypted chunk of file).

    IMPORTANT: Slices are NOT group-wrapped or signed. Access control and integrity
    are enforced via the message_attachment event which contains the root_hash.

    Args:
        file_id: ID of the file this slice belongs to
        slice_number: Index of this slice (0-based)
        nonce: 24-byte nonce used for encryption
        ciphertext: Encrypted bytes (max 450 bytes)
        poly_tag: 16-byte AEAD authentication tag
        peer_id: Local peer creating this event
        t_ms: Timestamp
        db: Database connection

    Returns:
        slice_event_id (BLAKE2b-128 of event)
    """
    if not _batch_mode:
        log.info(f"file_slice.create() file_id={file_id}, slice_number={slice_number}, "
                 f"ciphertext_size={len(ciphertext)}B")

    # Build event structure (NO signatures, NO wrapping)
    # Integrity verified via root_hash in message_attachment
    event_data = {
        'type': 'file_slice',
        'file_id': file_id,
        'slice_number': slice_number,
        'nonce': crypto.b64encode(nonce),
        'ciphertext': crypto.b64encode(ciphertext),
        'poly_tag': crypto.b64encode(poly_tag),
        'created_at': t_ms
    }

    # Canonicalize (no signing, no encryption)
    canonical = crypto.canonicalize_json(event_data)

    # Store as plain event (no wrapping at all)
    slice_event_id = store.event(canonical, peer_id, t_ms, db)

    if not _batch_mode:
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

    # Insert or ignore (now includes event_id for sync_file)
    safedb.execute(
        """INSERT OR IGNORE INTO file_slices
           (file_id, slice_number, nonce, ciphertext, poly_tag, event_id, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (file_id, slice_number, nonce, ciphertext, poly_tag, event_id, recorded_by, recorded_at)
    )

    # Record dependency: slice depends on file (for cascading deletion)
    safedb.execute(
        """INSERT OR IGNORE INTO event_dependencies
           (child_event_id, parent_event_id, recorded_by, dependency_type)
           VALUES (?, ?, ?, ?)""",
        (event_id, file_id, recorded_by, 'file')
    )

    log.debug(f"file_slice.project() projected slice {file_id[:20]}.../{slice_number}")


def batch_create_slices(file_id: str, slices_data: list[tuple], peer_id: str,
                        t_ms: int, db: Any, defer_bucket_rebuild: bool = False) -> int:
    """Efficiently create many file slices in batch mode.

    Uses optimized batch storage without immediate projection for massive performance gains.
    Slices are not signed - integrity is verified via root_hash in message_attachment.

    Args:
        file_id: ID of the file these slices belong to
        slices_data: List of (slice_number, nonce, ciphertext, poly_tag) tuples
        peer_id: Local peer creating these events
        t_ms: Timestamp
        db: Database connection
        defer_bucket_rebuild: If True, skip negentropy bucket rebuild (caller must call
                              negentropy.rebuild_buckets_for_peer() after all batches)

    Returns:
        Number of slices created
    """
    global _batch_mode
    from core import store
    from core.db import create_safe_db

    if not slices_data:
        return 0

    # Build all event blobs upfront (no signing - integrity via root_hash)
    event_blobs = []
    for slice_number, slice_nonce, ciphertext, poly_tag in slices_data:
        event_data = {
            'type': 'file_slice',
            'file_id': file_id,
            'slice_number': slice_number,
            'nonce': crypto.b64encode(slice_nonce),
            'ciphertext': crypto.b64encode(ciphertext),
            'poly_tag': crypto.b64encode(poly_tag),
            'created_at': t_ms
        }
        canonical = crypto.canonicalize_json(event_data)
        event_blobs.append(canonical)

    # Store all events in bulk AND project them directly (skipping recorded.project overhead)
    old_store_batch = store._batch_mode
    store._batch_mode = True

    try:
        event_ids = store.batch_store_events(event_blobs, peer_id, t_ms, db)

        # Project all slices efficiently using executemany (bulk insert)
        # Build batch data for file_slices table
        slice_batch = [
            (file_id, slice_number, slice_nonce, ciphertext, poly_tag, event_id, peer_id, t_ms)
            for event_id, (slice_number, slice_nonce, ciphertext, poly_tag) in zip(event_ids, slices_data)
        ]

        db._conn.executemany(
            """INSERT OR IGNORE INTO file_slices
               (file_id, slice_number, nonce, ciphertext, poly_tag, event_id, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            slice_batch
        )

        # Build batch data for event_dependencies table
        dep_batch = [
            (event_id, file_id, peer_id, 'file')
            for event_id in event_ids
        ]

        db._conn.executemany(
            """INSERT OR IGNORE INTO event_dependencies
               (child_event_id, parent_event_id, recorded_by, dependency_type)
               VALUES (?, ?, ?, ?)""",
            dep_batch
        )

        # Mark slices as shareable using batch function for efficiency
        # File slices are regular shareable events - they sync like any other event type
        # Access control is enforced at message_attachment level (group-encrypted)
        from events.network import negentropy
        # Build batch: (event_id, created_at, recorded_at)
        shareable_batch = [(event_id, None, t_ms) for event_id in event_ids]
        # Defer bucket computation for efficiency - we'll rebuild once at the end
        negentropy.add_shareable_events_batch(shareable_batch, peer_id, db, defer_buckets=True)

        # Rebuild all bucket hashes in one efficient pass (unless caller defers)
        if not defer_bucket_rebuild:
            negentropy.rebuild_buckets_for_peer(db, peer_id)

        log.info(f"file_slice.batch_create_slices() created {len(event_ids)} slices for file {file_id[:20]}...")
        return len(event_ids)

    finally:
        store._batch_mode = old_store_batch
