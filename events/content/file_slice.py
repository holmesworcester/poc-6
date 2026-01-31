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

# Wire format constants
WIRE_TYPE_CODE = 0x08  # TYPE_FILE_SLICE
# file_slice has a different wire format - no separate plaintext size
FILE_SLICE_CIPHERTEXT_SIZE = 450
FILE_SLICE_NONCE_SIZE = 24
FILE_SLICE_TAG_SIZE = 16

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

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


# Wire format functions - file_slice uses a special format, not the standard envelope

def _require_len(name: str, value: bytes, expected: int) -> bytes:
    if len(value) != expected:
        raise ValueError(f"{name} must be {expected} bytes, got {len(value)}")
    return value


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a file_slice wire format."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    return data[0] == 1 and data[1] == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    file_id: bytes,
    slice_number: int,
    nonce: bytes,
    ciphertext: bytes,
    poly_tag: bytes,
) -> bytes:
    """Encode a file_slice wire event.

    file_slice has a special format - not the standard envelope:
    - byte 0: version (1)
    - byte 1: type (TYPE_FILE_SLICE = 0x08)
    - bytes 2-17: file_id (16 bytes)
    - bytes 18-21: slice_number (u32 little-endian)
    - bytes 22-45: nonce (24 bytes)
    - bytes 46-495: padded ciphertext (450 bytes)
    - bytes 496-511: poly_tag (16 bytes)
    """
    _require_len("file_id", file_id, 16)
    if slice_number < 0 or slice_number > 0xFFFFFFFF:
        raise ValueError("slice_number must fit in u32")
    _require_len("nonce", nonce, FILE_SLICE_NONCE_SIZE)
    _require_len("poly_tag", poly_tag, FILE_SLICE_TAG_SIZE)
    ciphertext = bytes(ciphertext)
    if len(ciphertext) > FILE_SLICE_CIPHERTEXT_SIZE:
        raise ValueError(
            f"ciphertext exceeds {FILE_SLICE_CIPHERTEXT_SIZE} bytes, got {len(ciphertext)}"
        )
    padded_ciphertext = ciphertext + (b"\x00" * (FILE_SLICE_CIPHERTEXT_SIZE - len(ciphertext)))
    blob = bytearray(wire_format.WIRE_SIZE)
    blob[0] = 1
    blob[1] = WIRE_TYPE_CODE
    blob[2:18] = file_id
    struct.pack_into("<I", blob, 18, slice_number)
    blob[22:46] = nonce
    blob[46:46 + FILE_SLICE_CIPHERTEXT_SIZE] = padded_ciphertext
    blob[496:512] = poly_tag
    return bytes(blob)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a file_slice wire event.

    Returns dict with type, file_id, slice_number, nonce, ciphertext, poly_tag
    (all ids/bytes as base64 strings).
    """
    if len(data) != wire_format.WIRE_SIZE:
        raise ValueError(f"file_slice must be {wire_format.WIRE_SIZE} bytes, got {len(data)}")
    if data[0] != 1 or data[1] != WIRE_TYPE_CODE:
        raise ValueError("unexpected version or type for file_slice")
    file_id = data[2:18]
    (slice_number,) = struct.unpack_from("<I", data, 18)
    nonce = data[22:46]
    ciphertext = data[46:46 + FILE_SLICE_CIPHERTEXT_SIZE]
    poly_tag = data[496:512]
    return {
        "type": EVENT_TYPE,
        "file_id": crypto.b64encode(file_id),
        "slice_number": slice_number,
        "nonce": crypto.b64encode(nonce),
        "ciphertext": crypto.b64encode(ciphertext),
        "poly_tag": crypto.b64encode(poly_tag),
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

    _wire_shadow_file_slice(file_id, slice_number, nonce_b64, ciphertext_b64, poly_tag_b64)

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


def _wire_shadow_file_slice(
    file_id: str,
    slice_number: int,
    nonce_b64: str,
    ciphertext_b64: str,
    poly_tag_b64: str,
) -> None:
    """Validate file_slice fields against the fixed-size wire layout."""
    plaintext = encode_wire_event(
        file_id=crypto.b64decode(file_id),
        slice_number=slice_number,
        nonce=crypto.b64decode(nonce_b64),
        ciphertext=crypto.b64decode(ciphertext_b64),
        poly_tag=crypto.b64decode(poly_tag_b64),
    )
    decoded = decode_wire_event(plaintext)
    if decoded["file_id"] != file_id:
        raise ValueError("wire shadow decode file_id mismatch")

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

    blob = encode_wire_event(
        file_id=crypto.b64decode(file_id),
        slice_number=slice_number,
        nonce=nonce,
        ciphertext=ciphertext,
        poly_tag=poly_tag,
    )
    slice_event_id = store.event(blob, peer_id, t_ms, db)

    if not _batch_mode:
        log.info(f"file_slice.create() created slice_event_id={slice_event_id[:20]}...")
    return slice_event_id


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
        blob = encode_wire_event(
            file_id=crypto.b64decode(file_id),
            slice_number=slice_number,
            nonce=slice_nonce,
            ciphertext=ciphertext,
            poly_tag=poly_tag,
        )
        event_blobs.append(blob)

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
        # defer_buckets controls whether we do incremental updates now or defer to caller
        # Incremental updates (defer_buckets=False) are efficient - only process new events
        negentropy.add_shareable_events_batch(shareable_batch, peer_id, db, defer_buckets=defer_bucket_rebuild)

        log.info(f"file_slice.batch_create_slices() created {len(event_ids)} slices for file {file_id[:20]}...")
        return len(event_ids)

    finally:
        store._batch_mode = old_store_batch
