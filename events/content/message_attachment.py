"""Message attachment event type - attaches files to messages with encryption metadata.

Attachments ARE group-wrapped (access control).
File descriptor data (enc_key, root_hash, etc.) is now embedded in this event
instead of a separate 'file' event.
"""

# Registry metadata
EVENT_TYPE = 'message_attachment'
SHAREABLE = True  # Attachments sync with messages
PROJECTION_TABLE = None  # No created_at lookup needed

# Wire format constants
WIRE_TYPE_CODE = 0x07  # TYPE_MESSAGE_ATTACHMENT
WIRE_PLAINTEXT_SIZE = 344  # MESSAGE_ATTACHMENT_PLAINTEXT_SIZE
FILENAME_MAX = 128
MIME_MAX = 32
NONCE_PREFIX_SIZE = 20
SECRET_SIZE = 32

# v2 event specification
EVENT_SPEC = {
    'encrypted': True,  # Group-wrapped via store.publish
    'signer': None,  # Signature verified manually via crypto.verify_signed_by_peer_shared
    'requires': {
        'message': {
            'source': 'table',
            'table': 'messages',
            'key': 'message_id',
            'fields': ['message_id', 'signed_by'],
        },
    },
    'optional': {},
    'cascade_on_delete': ['message'],  # Delete attachment when message is deleted
}

from typing import Any
import base64
import io
import logging
import struct
from PIL import Image
from core import crypto
from core import store
from core import wire_format
from core.projection.types import ProjectorResult, WriteOp
from events.identity import peer_shared, peer
from events.group import group
from events.content import file_slice, message
from core.db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for message_attachment event type

def encode_plaintext(
    message_id: bytes,
    file_id: bytes,
    blob_bytes: int,
    total_slices: int,
    nonce_prefix: bytes,
    enc_key: bytes,
    root_hash: bytes,
    filename: str | bytes | None,
    mime_type: str | bytes | None,
) -> bytes:
    """Encode a message_attachment payload plaintext (pre-encryption)."""
    wire_format._require_len("message_id", message_id, 16)
    wire_format._require_len("file_id", file_id, 16)
    wire_format._require_len("nonce_prefix", nonce_prefix, NONCE_PREFIX_SIZE)
    wire_format._require_len("enc_key", enc_key, SECRET_SIZE)
    wire_format._require_len("root_hash", root_hash, 32)
    if blob_bytes < 0:
        raise ValueError("blob_bytes must be non-negative")
    if total_slices < 0 or total_slices > 0xFFFFFFFF:
        raise ValueError("total_slices must fit in u32")

    if filename is None:
        filename_bytes = b""
    elif isinstance(filename, str):
        filename_bytes = filename.encode("utf-8")
    else:
        filename_bytes = bytes(filename)

    if mime_type is None:
        mime_bytes = b""
    elif isinstance(mime_type, str):
        mime_bytes = mime_type.encode("utf-8")
    else:
        mime_bytes = bytes(mime_type)

    if len(filename_bytes) > FILENAME_MAX:
        raise ValueError(f"filename exceeds {FILENAME_MAX} bytes, got {len(filename_bytes)}")
    if len(mime_bytes) > MIME_MAX:
        raise ValueError(f"mime_type exceeds {MIME_MAX} bytes, got {len(mime_bytes)}")

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = message_id
    payload[16:32] = file_id
    struct.pack_into("<Q", payload, 32, blob_bytes)
    struct.pack_into("<I", payload, 40, total_slices)
    payload[44:44 + NONCE_PREFIX_SIZE] = nonce_prefix
    payload[64:96] = enc_key
    payload[96:128] = root_hash
    struct.pack_into("<H", payload, 128, len(filename_bytes))
    payload[130:130 + len(filename_bytes)] = filename_bytes
    mime_len_offset = 130 + FILENAME_MAX
    struct.pack_into("<H", payload, mime_len_offset, len(mime_bytes))
    payload[mime_len_offset + 2:mime_len_offset + 2 + len(mime_bytes)] = mime_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_attachment payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"message_attachment plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    message_id = data[0:16]
    file_id = data[16:32]
    (blob_bytes,) = struct.unpack_from("<Q", data, 32)
    (total_slices,) = struct.unpack_from("<I", data, 40)
    nonce_prefix = data[44:44 + NONCE_PREFIX_SIZE]
    enc_key = data[64:96]
    root_hash = data[96:128]
    (filename_len,) = struct.unpack_from("<H", data, 128)
    if filename_len > FILENAME_MAX:
        raise ValueError(f"filename_len exceeds {FILENAME_MAX}, got {filename_len}")
    filename_bytes = data[130:130 + filename_len]
    (mime_len,) = struct.unpack_from("<H", data, 130 + FILENAME_MAX)
    if mime_len > MIME_MAX:
        raise ValueError(f"mime_len exceeds {MIME_MAX}, got {mime_len}")
    mime_bytes = data[132 + FILENAME_MAX:132 + FILENAME_MAX + mime_len]

    if filename_len:
        try:
            filename = filename_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("filename is not valid utf-8") from exc
    else:
        filename = None
    if mime_len:
        try:
            mime_type = mime_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("mime_type is not valid utf-8") from exc
    else:
        mime_type = None
    return {
        "message_id": message_id,
        "file_id": file_id,
        "blob_bytes": blob_bytes,
        "total_slices": total_slices,
        "nonce_prefix": nonce_prefix,
        "enc_key": enc_key,
        "root_hash": root_hash,
        "filename": filename,
        "mime_type": mime_type,
    }


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt message_attachment plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"message_attachment plaintext must be {WIRE_PLAINTEXT_SIZE} bytes")
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message_attachment payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != WIRE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message_attachment payload")
    payload = key_id + nonce + ciphertext
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to message_attachment plaintext."""
    if key_data.get("type") != "symmetric":
        raise ValueError("message_attachment payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a message_attachment wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    message_id_b64: str,
    file_id_b64: str,
    blob_bytes: int,
    total_slices: int,
    nonce_prefix_b64: str,
    enc_key_b64: str,
    root_hash_b64: str,
    filename: str | None,
    mime_type: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete message_attachment wire event."""
    message_id = crypto.b64decode(message_id_b64)
    file_id = crypto.b64decode(file_id_b64)
    nonce_prefix = crypto.b64decode(nonce_prefix_b64)
    enc_key = crypto.b64decode(enc_key_b64)
    root_hash = crypto.b64decode(root_hash_b64)
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_plaintext(
        message_id=message_id,
        file_id=file_id,
        blob_bytes=blob_bytes,
        total_slices=total_slices,
        nonce_prefix=nonce_prefix,
        enc_key=enc_key,
        root_hash=root_hash,
        filename=filename,
        mime_type=mime_type,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_payload(plaintext, key_data)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode a message_attachment wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        return None, []
    if header.flags & wire_format.FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_payload(payload, key_data)
    else:
        plaintext = payload[:WIRE_PLAINTEXT_SIZE]

    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "message_id": crypto.b64encode(decoded["message_id"]),
        "file_id": crypto.b64encode(decoded["file_id"]),
        "filename": decoded["filename"],
        "mime_type": decoded["mime_type"],
        "blob_bytes": decoded["blob_bytes"],
        "total_slices": decoded["total_slices"],
        "nonce_prefix": crypto.b64encode(decoded["nonce_prefix"]),
        "enc_key": crypto.b64encode(decoded["enc_key"]),
        "root_hash": crypto.b64encode(decoded["root_hash"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data, []



def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for message_attachment events.

    Validates that attachment creator matches message creator,
    then inserts into message_attachments and event_dependencies.
    """
    event_data = ctx.event_data

    message_id = event_data.get('message_id')
    file_id = event_data.get('file_id')
    filename = event_data.get('filename')
    mime_type = event_data.get('mime_type')
    signed_by = event_data.get('signed_by')

    # File descriptor fields
    blob_bytes = event_data.get('blob_bytes')
    nonce_prefix_b64 = event_data.get('nonce_prefix')
    enc_key_b64 = event_data.get('enc_key')
    root_hash_b64 = event_data.get('root_hash')
    total_slices = event_data.get('total_slices')

    # Validate required fields
    if not all([message_id, file_id, signed_by, blob_bytes is not None,
                nonce_prefix_b64, enc_key_b64, root_hash_b64, total_slices is not None]):
        log.warning(f"message_attachment.project_pure() missing required fields")
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_message_attachment(
        message_id=message_id,
        file_id=file_id,
        blob_bytes=blob_bytes,
        total_slices=total_slices,
        nonce_prefix_b64=nonce_prefix_b64,
        enc_key_b64=enc_key_b64,
        root_hash_b64=root_hash_b64,
        filename=filename,
        mime_type=mime_type,
    )

    # Validate message exists and attachment creator matches message creator
    message_row = ctx.deps.get('message')
    if not message_row:
        log.warning(f"message_attachment.project_pure() message not found")
        return ProjectorResult(writes=tuple(), valid_event=False)

    if message_row.get('signed_by') != signed_by:
        log.warning(f"message_attachment.project_pure() signer mismatch: "
                   f"attachment={signed_by[:20] if signed_by else 'None'}... "
                   f"message={message_row.get('signed_by', 'None')[:20]}...")
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Decode file descriptor fields
    nonce_prefix = crypto.b64decode(nonce_prefix_b64)
    enc_key = crypto.b64decode(enc_key_b64)
    root_hash = crypto.b64decode(root_hash_b64)

    writes = (
        WriteOp(
            op='insert',
            table='message_attachments',
            values={
                'message_id': message_id,
                'file_id': file_id,
                'filename': filename,
                'mime_type': mime_type,
                'blob_bytes': blob_bytes,
                'nonce_prefix': nonce_prefix,
                'enc_key': enc_key,
                'root_hash': root_hash,
                'total_slices': total_slices,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
        WriteOp(
            op='insert',
            table='event_dependencies',
            values={
                'child_event_id': ctx.event_id,
                'parent_event_id': message_id,
                'recorded_by': ctx.recorded_by,
                'dependency_type': 'message',
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True, commands=())


def _wire_shadow_message_attachment(
    *,
    message_id: str,
    file_id: str,
    blob_bytes: int,
    total_slices: int,
    nonce_prefix_b64: str,
    enc_key_b64: str,
    root_hash_b64: str,
    filename: str | None,
    mime_type: str | None,
) -> None:
    """Validate message_attachment fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        message_id=crypto.b64decode(message_id),
        file_id=crypto.b64decode(file_id),
        blob_bytes=blob_bytes,
        total_slices=total_slices,
        nonce_prefix=crypto.b64decode(nonce_prefix_b64),
        enc_key=crypto.b64decode(enc_key_b64),
        root_hash=crypto.b64decode(root_hash_b64),
        filename=filename,
        mime_type=mime_type,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["file_id"] != crypto.b64decode(file_id):
        raise ValueError("wire shadow decode file_id mismatch")


SLICE_SIZE = 450  # bytes - matches ideal protocol design


def create(peer_id: str, message_id: str, file_data: bytes,
           filename: str | None, mime_type: str | None,
           t_ms: int, db: Any) -> dict[str, Any]:
    """Create message_attachment event with file data and all metadata.

    Creates file_slice events and a message_attachment event containing:
    - message_id, filename, mime_type (attachment fields)
    - file_id, blob_bytes, enc_key, nonce_prefix, root_hash, total_slices (file descriptor fields)

    This event IS group-encrypted (access control).

    Args:
        peer_id: Local peer creating this event
        message_id: Message being attached to
        file_data: Raw file bytes
        filename: Optional filename
        mime_type: Optional MIME type
        t_ms: Timestamp
        db: Database connection

    Returns:
        {
            'file_id': file_id,
            'slice_count': number_of_slices,
            'blob_bytes': len(file_data),
            'attachment_event_id': attachment_event_id
        }
    """
    # Only log if not a large file (to avoid spam for batch operations)
    if len(file_data) < 10 * 1024 * 1024:  # Log for files < 10 MB
        log.info(f"message_attachment.create() message_id={message_id[:20]}..., "
                 f"file_size={len(file_data)}B")
    else:
        log.debug(f"message_attachment.create() message_id={message_id[:20]}..., "
                  f"file_size={len(file_data)}B")

    # Get message to verify access and get group_id
    message_row = message.get(message_id, peer_id, db)
    if not message_row:
        raise ValueError(f"Message {message_id} not found for peer {peer_id}")

    group_id = message_row['group_id']

    # Get peer_shared_id for signed_by field
    identity = peer_shared.get_self(peer_id, db)
    if not identity or not identity['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set")

    peer_shared_id = identity['peer_shared_id']

    # Step 1-2: Generate encryption key and nonce prefix
    enc_key = crypto.generate_secret()  # 32 bytes
    nonce_prefix = crypto.generate_secret()[:20]  # Use first 20 bytes

    # Step 3: Split file into slices and encrypt
    slice_ciphertexts = []  # For computing root_hash and file_id
    slices_to_create = []  # Store for creation after computing file_id

    for slice_number in range(0, len(file_data), SLICE_SIZE):
        plaintext_slice = file_data[slice_number:slice_number + SLICE_SIZE]
        if len(plaintext_slice) < SLICE_SIZE:
            plaintext_slice = plaintext_slice.ljust(SLICE_SIZE, b"\x00")

        # Derive nonce for this slice
        slice_nonce = crypto.derive_slice_nonce(nonce_prefix, slice_number)

        # Encrypt slice
        ciphertext, poly_tag = crypto.encrypt_file_slice(plaintext_slice, enc_key, slice_nonce)

        # Save for root_hash computation
        slice_ciphertexts.append(ciphertext)
        slices_to_create.append((slice_number, slice_nonce, ciphertext, poly_tag))

    # Step 4: Compute file_id from full ciphertext
    full_ciphertext = b''.join(slice_ciphertexts)
    file_id = crypto.compute_file_id(full_ciphertext)

    # Step 5: Create file_slice events in batch (now that we have file_id)
    # Note: slices are not signed - integrity verified via root_hash
    slice_count = file_slice.batch_create_slices(
        file_id=file_id,
        slices_data=slices_to_create,
        peer_id=peer_id,
        t_ms=t_ms,
        db=db
    )

    if len(file_data) < 10 * 1024 * 1024:  # Log for files < 10 MB
        log.info(f"message_attachment.create() created {slice_count} slices, "
                 f"file_id={file_id[:20]}...")
    else:
        log.debug(f"message_attachment.create() created {slice_count} slices, "
                  f"file_id={file_id[:20]}...")

    # Step 6: Compute root_hash
    root_hash = crypto.compute_root_hash(slice_ciphertexts)

    private_key = peer.get_private_key(peer_id, peer_id, db)
    key_data = group.pick_key(group_id, peer_id, db)
    blob = encode_wire_event(
        message_id_b64=message_id,
        file_id_b64=file_id,
        blob_bytes=len(file_data),
        total_slices=slice_count,
        nonce_prefix_b64=crypto.b64encode(nonce_prefix),
        enc_key_b64=crypto.b64encode(enc_key),
        root_hash_b64=crypto.b64encode(root_hash),
        filename=filename,
        mime_type=mime_type,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )
    attachment_event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"message_attachment.create() created attachment_event_id={attachment_event_id[:20]}...")

    # Note: consolidation will happen automatically during projection when the
    # message_attachment is projected and get_file_download_progress is called

    return {
        'file_id': file_id,
        'slice_count': slice_count,
        'blob_bytes': len(file_data),
        'attachment_event_id': attachment_event_id,
        'file_id_for_consolidation': file_id  # Hint for caller to consolidate if needed
    }


def create_from_file(peer_id: str, message_id: str, file_path: str,
                     filename: str | None, mime_type: str | None,
                     t_ms: int, db: Any) -> dict[str, Any]:
    """Create message_attachment event by streaming from a file path.

    This is the memory-efficient alternative to create() for large files.
    Streams the file from disk, avoiding loading the entire file into memory.

    Memory usage: <10MB regardless of file size (processes in batches).

    Algorithm:
    1. Pass 1: Stream file, encrypt slices, write to temp file, compute hashes incrementally
    2. Pass 2: Read temp file in batches, create file_slice events
    3. Create message_attachment event with computed metadata

    Args:
        peer_id: Local peer creating this event
        message_id: Message being attached to
        file_path: Path to file on disk
        filename: Optional filename (defaults to basename of file_path)
        mime_type: Optional MIME type
        t_ms: Timestamp
        db: Database connection

    Returns:
        Same as create(): {
            'file_id': file_id,
            'slice_count': number_of_slices,
            'blob_bytes': file_size,
            'attachment_event_id': attachment_event_id
        }
    """
    import os
    import struct
    import tempfile
    from pathlib import Path

    file_path = Path(file_path)
    file_size = file_path.stat().st_size

    # Use basename as filename if not provided
    if filename is None:
        filename = file_path.name

    log.info(f"message_attachment.create_from_file() message_id={message_id[:20]}..., "
             f"file_path={file_path.name}, file_size={file_size:,}B")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get message to verify access and get group_id
    message_row = safedb.query_one(
        "SELECT group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, peer_id)
    )
    if not message_row:
        raise ValueError(f"Message {message_id} not found for peer {peer_id}")

    group_id = message_row['group_id']

    # Get peer_shared_id for signed_by field
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set")

    peer_shared_id = peer_self_row['peer_shared_id']

    # Generate encryption key and nonce prefix
    enc_key = crypto.generate_secret()  # 32 bytes
    nonce_prefix = crypto.generate_secret()[:20]  # Use first 20 bytes

    # Create temp file for encrypted slice data
    temp_file = tempfile.NamedTemporaryFile(delete=False, mode='wb')
    temp_file_path = temp_file.name

    # Incremental hashers for file_id (16 bytes) and root_hash (32 bytes)
    file_id_hasher = crypto.HashBuilder(size=16)
    root_hash_hasher = crypto.HashBuilder(size=32)

    slice_count = 0

    try:
        # ===== PASS 1: Stream, encrypt, hash, write to temp file =====
        log.debug(f"create_from_file() Pass 1: streaming and encrypting...")

        with open(file_path, 'rb') as f:
            slice_offset = 0  # Byte offset (used as slice_number)
            while True:
                plaintext_slice = f.read(SLICE_SIZE)
                if not plaintext_slice:
                    break

                # Derive nonce for this slice
                slice_nonce = crypto.derive_slice_nonce(nonce_prefix, slice_offset)

                # Encrypt slice
                if len(plaintext_slice) < SLICE_SIZE:
                    plaintext_slice = plaintext_slice.ljust(SLICE_SIZE, b"\x00")
                ciphertext, poly_tag = crypto.encrypt_file_slice(plaintext_slice, enc_key, slice_nonce)

                # Update incremental hashes
                file_id_hasher.update(ciphertext)
                root_hash_hasher.update(ciphertext)

                # Write to temp file: slice_offset(4) + nonce_len(1) + nonce + ct_len(2) + ciphertext + poly_tag(16)
                temp_file.write(struct.pack('<I', slice_offset))  # 4 bytes
                temp_file.write(struct.pack('<B', len(slice_nonce)))  # 1 byte
                temp_file.write(slice_nonce)
                temp_file.write(struct.pack('<H', len(ciphertext)))  # 2 bytes
                temp_file.write(ciphertext)
                temp_file.write(poly_tag)  # 16 bytes

                slice_offset += SLICE_SIZE
                slice_count += 1

        temp_file.close()

        # Compute final hashes
        file_id = crypto.b64encode(file_id_hasher.digest())
        root_hash = root_hash_hasher.digest()

        log.debug(f"create_from_file() Pass 1 complete: {slice_count} slices, file_id={file_id[:20]}...")

        # ===== PASS 2: Read temp file in batches, create slice events =====
        log.debug(f"create_from_file() Pass 2: creating slice events in batches...")

        BATCH_SIZE = 1000
        slices_batch = []
        slices_created = 0

        with open(temp_file_path, 'rb') as f:
            for _ in range(slice_count):
                # Read: slice_offset(4) + nonce_len(1) + nonce + ct_len(2) + ciphertext + poly_tag(16)
                slice_offset = struct.unpack('<I', f.read(4))[0]
                nonce_len = struct.unpack('<B', f.read(1))[0]
                nonce = f.read(nonce_len)
                ct_len = struct.unpack('<H', f.read(2))[0]
                ciphertext = f.read(ct_len)
                poly_tag = f.read(16)

                slices_batch.append((slice_offset, nonce, ciphertext, poly_tag))

                if len(slices_batch) >= BATCH_SIZE:
                    # Defer bucket rebuild for all batches - we'll do one rebuild at the end
                    file_slice.batch_create_slices(
                        file_id=file_id,
                        slices_data=slices_batch,
                        peer_id=peer_id,
                        t_ms=t_ms,
                        db=db,
                        defer_bucket_rebuild=True
                    )
                    slices_created += len(slices_batch)
                    log.debug(f"create_from_file() created {slices_created}/{slice_count} slices...")
                    slices_batch.clear()

            # Final batch
            if slices_batch:
                file_slice.batch_create_slices(
                    file_id=file_id,
                    slices_data=slices_batch,
                    peer_id=peer_id,
                    t_ms=t_ms,
                    db=db,
                )
                slices_created += len(slices_batch)

        # In bisection protocol, hashes are computed on demand - no bucket rebuild needed

        log.info(f"create_from_file() created {slices_created} slices, file_id={file_id[:20]}...")

    finally:
        # Clean up temp file
        try:
            os.unlink(temp_file_path)
        except Exception:
            pass

    # ===== Create message_attachment event =====
    # Sign the event
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Get group key for encryption
    key_data = group.pick_key(group_id, peer_id, db)

    blob = encode_wire_event(
        message_id_b64=message_id,
        file_id_b64=file_id,
        blob_bytes=file_size,
        total_slices=slice_count,
        nonce_prefix_b64=crypto.b64encode(nonce_prefix),
        enc_key_b64=crypto.b64encode(enc_key),
        root_hash_b64=crypto.b64encode(root_hash),
        filename=filename,
        mime_type=mime_type,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )

    # Store event
    attachment_event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"create_from_file() created attachment_event_id={attachment_event_id[:20]}...")

    return {
        'file_id': file_id,
        'slice_count': slice_count,
        'blob_bytes': file_size,
        'attachment_event_id': attachment_event_id,
        'file_id_for_consolidation': file_id
    }


def compress_image_if_needed(file_data: bytes, mime_type: str | None,
                             target_size_kb: int = 200,
                             max_dimension: int = 2048) -> tuple[bytes, dict[str, Any]]:
    """Compress image to target size if it's an image and over the limit.

    Strategy:
    1. Check if file is an image by mime_type
    2. If not image or already small enough, return original
    3. Resize if dimensions too large (maintains aspect ratio)
    4. Progressively reduce quality (85 → 75 → 65 → 55 → 45 → 40)
    5. If still too large, try converting to WebP (25-35% smaller)
    6. Return compressed data + metadata

    Args:
        file_data: Original file bytes
        mime_type: MIME type (e.g., 'image/jpeg', 'image/png')
        target_size_kb: Target size in kilobytes (default: 200KB)
        max_dimension: Maximum width/height in pixels (default: 2048)

    Returns:
        (compressed_bytes, metadata_dict)
        metadata_dict contains:
            - compressed: bool (was compression applied)
            - original_size: int (bytes)
            - final_size: int (bytes)
            - compression_ratio: float (original/final, e.g., 2.5 = 2.5x smaller)
            - method: str ('none', 'quality_reduction', 'webp_conversion', 'resize')
            - original_format: str
            - final_format: str

    Example:
        compressed_data, stats = compress_image_if_needed(
            file_data=image_bytes,
            mime_type='image/jpeg',
            target_size_kb=200
        )
        # stats = {
        #     'compressed': True,
        #     'original_size': 2048000,
        #     'final_size': 195000,
        #     'compression_ratio': 10.5,
        #     'method': 'quality_reduction',
        #     'original_format': 'JPEG',
        #     'final_format': 'JPEG'
        # }
    """
    original_size = len(file_data)
    target_size_bytes = target_size_kb * 1024

    # Initialize metadata
    metadata = {
        'compressed': False,
        'original_size': original_size,
        'final_size': original_size,
        'compression_ratio': 1.0,
        'method': 'none',
        'original_format': 'unknown',
        'final_format': 'unknown'
    }

    # Check if it's an image
    if not mime_type or not mime_type.startswith('image/'):
        log.debug(f"compress_image_if_needed() not an image (mime={mime_type}), skipping")
        return file_data, metadata

    # Check if already small enough
    if original_size <= target_size_bytes:
        log.debug(f"compress_image_if_needed() already small ({original_size}B <= {target_size_bytes}B), skipping")
        return file_data, metadata

    log.info(f"compress_image_if_needed() compressing {original_size:,}B image to target {target_size_bytes:,}B")

    try:
        # Open image
        img = Image.open(io.BytesIO(file_data))
        original_format = img.format or 'JPEG'
        metadata['original_format'] = original_format

        # Resize if too large
        if max(img.size) > max_dimension:
            img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            log.info(f"compress_image_if_needed() resized to {img.size}")
            metadata['method'] = 'resize'

        # Convert RGBA/P to RGB for JPEG compatibility
        if img.mode in ('RGBA', 'LA', 'P'):
            if img.mode == 'P':
                img = img.convert('RGBA')
            background = Image.new('RGB', img.size, (255, 255, 255))
            if 'A' in img.mode:
                background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert('RGB')

        # Try progressive quality reduction for JPEG
        quality_levels = [85, 75, 65, 55, 45, 40]
        best_result = None
        best_size = float('inf')

        for quality in quality_levels:
            output = io.BytesIO()
            img.save(output, format='JPEG', quality=quality, optimize=True)
            size = output.tell()

            log.debug(f"compress_image_if_needed() JPEG quality={quality} → {size:,}B")

            if size <= target_size_bytes:
                best_result = output.getvalue()
                best_size = size
                metadata['method'] = 'quality_reduction'
                metadata['final_format'] = 'JPEG'
                break

            if size < best_size:
                best_result = output.getvalue()
                best_size = size

        # If JPEG still too large, try WebP (typically 25-35% smaller)
        if best_size > target_size_bytes:
            log.info(f"compress_image_if_needed() JPEG still too large ({best_size:,}B), trying WebP")

            for quality in quality_levels:
                output = io.BytesIO()
                img.save(output, format='WEBP', quality=quality, method=4)
                size = output.tell()

                log.debug(f"compress_image_if_needed() WebP quality={quality} → {size:,}B")

                if size <= target_size_bytes:
                    best_result = output.getvalue()
                    best_size = size
                    metadata['method'] = 'webp_conversion'
                    metadata['final_format'] = 'WEBP'
                    break

                if size < best_size:
                    best_result = output.getvalue()
                    best_size = size
                    metadata['final_format'] = 'WEBP'

        # Use best result (or original if compression failed)
        if best_result and best_size < original_size:
            metadata['compressed'] = True
            metadata['final_size'] = best_size
            metadata['compression_ratio'] = original_size / best_size
            log.info(f"compress_image_if_needed() compressed {original_size:,}B → {best_size:,}B "
                    f"({metadata['compression_ratio']:.1f}x, method={metadata['method']})")
            return best_result, metadata
        else:
            log.warning(f"compress_image_if_needed() compression failed, using original")
            return file_data, metadata

    except Exception as e:
        log.error(f"compress_image_if_needed() error: {e}, using original file")
        return file_data, metadata


def create_from_base64(peer_id: str, message_id: str, base64_data: str,
                       mime_type: str | None, filename: str | None,
                       t_ms: int, db: Any, auto_compress: bool = True) -> dict[str, Any]:
    """Create message attachment from base64-encoded file data.

    This is useful for frontend file uploads where files are sent as base64 strings.
    Decodes base64 and calls the regular create() function.

    Args:
        peer_id: Peer creating the attachment
        message_id: Message being attached to
        base64_data: Base64-encoded file bytes (without data URI prefix)
        mime_type: MIME type (e.g., 'image/png', 'application/pdf')
        filename: Optional filename
        t_ms: Timestamp
        db: Database connection
        auto_compress: If True, automatically compress images to 200KB (default: True)

    Returns:
        Same as create(): {
            'file_id': str,
            'slice_count': int,
            'root_hash': str (base64),
            'compressed': bool (if compression was applied),
            'original_size': int (original bytes before compression),
            'final_size': int (final bytes after compression),
            'compression_ratio': float (if compressed)
        }

    Raises:
        ValueError: If base64_data is invalid

    Example:
        # Frontend sends base64 string
        result = create_from_base64(
            peer_id=alice['peer_id'],
            message_id=message_id,
            base64_data='iVBORw0KGgoAAAANS...',  # No 'data:...' prefix
            mime_type='image/png',
            filename='photo.png',
            t_ms=5000,
            db=db,
            auto_compress=True  # Default
        )
        # Returns: {
        #     'file_id': '...',
        #     'slice_count': 10,
        #     'root_hash': '...',
        #     'compressed': True,
        #     'original_size': 512000,
        #     'final_size': 195000,
        #     'compression_ratio': 2.6
        # }
    """
    log.debug(f"message_attachment.create_from_base64() peer_id={peer_id[:20]}..., "
              f"message_id={message_id[:20]}..., data_len={len(base64_data)}, auto_compress={auto_compress}")

    # Decode base64 with validation
    try:
        file_data = base64.b64decode(base64_data, validate=True)
    except Exception as e:
        log.error(f"create_from_base64() invalid base64: {e}")
        raise ValueError(f"Invalid base64 data: {e}")

    log.info(f"create_from_base64() decoded {len(file_data)}B from {len(base64_data)} base64 chars")

    # Compress if requested and it's an image
    compression_metadata = {}
    if auto_compress:
        file_data, compression_metadata = compress_image_if_needed(file_data, mime_type)

    # Call regular create function
    result = create(
        peer_id=peer_id,
        message_id=message_id,
        file_data=file_data,
        filename=filename,
        mime_type=mime_type,
        t_ms=t_ms,
        db=db
    )

    # Add compression metadata to result
    if compression_metadata.get('compressed'):
        result.update({
            'compressed': compression_metadata['compressed'],
            'original_size': compression_metadata['original_size'],
            'final_size': compression_metadata['final_size'],
            'compression_ratio': compression_metadata['compression_ratio'],
            'compression_method': compression_metadata['method']
        })

    return result


def create_from_data_uri(peer_id: str, message_id: str, data_uri: str,
                        filename: str | None, t_ms: int, db: Any,
                        auto_compress: bool = True) -> dict[str, Any]:
    """Create message attachment from data URI string.

    Parses data URI to extract mime_type and base64 data, then creates attachment.
    Data URI format: data:{mime_type};base64,{base64_data}

    Args:
        peer_id: Peer creating the attachment
        message_id: Message being attached to
        data_uri: Full data URI (e.g., 'data:image/png;base64,iVBORw...')
        filename: Optional filename (not extracted from data URI)
        t_ms: Timestamp
        db: Database connection
        auto_compress: If True, automatically compress images to 200KB (default: True)

    Returns:
        Same as create(): {
            'file_id': str,
            'slice_count': int,
            'root_hash': str (base64),
            'compressed': bool (if compression was applied),
            'original_size': int,
            'final_size': int,
            'compression_ratio': float
        }

    Raises:
        ValueError: If data_uri format is invalid

    Example:
        result = create_from_data_uri(
            peer_id=alice['peer_id'],
            message_id=message_id,
            data_uri='data:image/png;base64,iVBORw0KGgoAAAANS...',
            filename='photo.png',
            t_ms=5000,
            db=db,
            auto_compress=True  # Default
        )
    """
    log.debug(f"message_attachment.create_from_data_uri() peer_id={peer_id[:20]}..., "
              f"message_id={message_id[:20]}..., uri_len={len(data_uri)}, auto_compress={auto_compress}")

    # Parse data URI
    if not data_uri.startswith('data:'):
        raise ValueError("Data URI must start with 'data:'")

    try:
        # Split on first comma: data:mime;base64,{data}
        header, base64_data = data_uri.split(',', 1)

        # Extract mime type from header
        # header format: "data:image/png;base64" or "data:image/png"
        mime_part = header[5:]  # Remove 'data:'

        if ';base64' in mime_part:
            mime_type = mime_part.split(';base64')[0]
        else:
            mime_type = mime_part

        # Handle empty mime type
        if not mime_type:
            mime_type = 'application/octet-stream'

    except Exception as e:
        log.error(f"create_from_data_uri() failed to parse data URI: {e}")
        raise ValueError(f"Invalid data URI format: {e}")

    log.info(f"create_from_data_uri() parsed mime_type={mime_type}, "
             f"base64_len={len(base64_data)}")

    # Use create_from_base64 to handle the rest
    return create_from_base64(
        peer_id=peer_id,
        message_id=message_id,
        base64_data=base64_data,
        mime_type=mime_type,
        filename=filename,
        t_ms=t_ms,
        db=db,
        auto_compress=auto_compress
    )


def is_file_complete(file_id: str, recorded_by: str, db: Any) -> bool:
    """Check if all slices for a file have been received.

    Args:
        file_id: File ID to check
        recorded_by: Peer ID who owns the file
        db: Database connection

    Returns:
        True if all slices received, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get total slices from attachment metadata
    attachment = safedb.query_one(
        "SELECT total_slices FROM message_attachments WHERE file_id = ? LIMIT 1",
        (file_id,)
    )
    if not attachment or not attachment['total_slices']:
        return False

    total_slices = attachment['total_slices']

    # Count received slices
    result = safedb.query_one(
        "SELECT COUNT(*) as count FROM file_slices WHERE file_id = ?",
        (file_id,)
    )
    received = result['count'] if result else 0

    return received >= total_slices


def consolidate_file_slices(file_id: str, recorded_by: str, db: Any) -> bool:
    """Consolidate all file slices into a single blob for fast reads.

    This is called when a file download completes. It concatenates all
    slice ciphertexts into a single BLOB stored in message_attachments.consolidated_blob.

    This provides 10-50x faster reads for large files by avoiding thousands
    of individual row lookups and Python loops.

    Args:
        file_id: File to consolidate
        recorded_by: Peer who owns the file
        db: Database connection

    Returns:
        True if consolidation succeeded, False otherwise
    """
    log.debug(f"consolidate_file_slices() file_id={file_id[:20]}..., recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get file metadata
    attachment_row = safedb.query_one(
        "SELECT total_slices, consolidated_blob FROM message_attachments "
        "WHERE file_id = ? AND recorded_by = ? LIMIT 1",
        (file_id, recorded_by)
    )

    if not attachment_row:
        log.warning(f"consolidate_file_slices() attachment not found: {file_id[:20]}...")
        return False

    # Skip if already consolidated
    if attachment_row['consolidated_blob'] is not None:
        log.debug(f"consolidate_file_slices() already consolidated: {file_id[:20]}...")
        return True

    total_slices = attachment_row['total_slices']

    # Get all slices in order
    slice_rows = safedb.query_all(
        "SELECT slice_number, nonce, ciphertext, poly_tag FROM file_slices "
        "WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number ASC",
        (file_id, recorded_by)
    )

    # Verify we have all slices
    if len(slice_rows) != total_slices:
        log.info(f"consolidate_file_slices() incomplete: have {len(slice_rows)}/{total_slices} slices")
        return False

    # Concatenate all slice data and track offsets
    # Format: [nonce(12) + ciphertext(var) + poly_tag(16)] repeated for each slice
    consolidated_parts = []
    offset_records = []  # Track offset info for each slice
    current_offset = 0

    for slice_row in slice_rows:
        nonce = slice_row['nonce']
        ciphertext = slice_row['ciphertext']
        poly_tag = slice_row['poly_tag']

        blob_offset_start = current_offset

        # Pack: nonce (12 bytes) + ciphertext (variable) + poly_tag (16 bytes)
        consolidated_parts.append(nonce)
        consolidated_parts.append(ciphertext)
        consolidated_parts.append(poly_tag)

        # Calculate offsets within the consolidated blob
        nonce_len = len(nonce)
        ciphertext_len = len(ciphertext)
        poly_tag_len = len(poly_tag)

        current_offset += nonce_len + ciphertext_len + poly_tag_len
        blob_offset_end = current_offset

        offset_records.append({
            'slice_number': slice_row['slice_number'],
            'ciphertext_len': ciphertext_len,
            'blob_offset_start': blob_offset_start,
            'blob_offset_end': blob_offset_end
        })

    consolidated_blob = b''.join(consolidated_parts)

    # Store consolidated blob (use safedb since message_attachments is subjective)
    safedb.execute(
        "UPDATE message_attachments SET consolidated_blob = ? "
        "WHERE file_id = ? AND recorded_by = ?",
        (consolidated_blob, file_id, recorded_by)
    )

    # Store offset information for each slice (enables deterministic unpacking)
    for offset_record in offset_records:
        safedb.execute(
            "UPDATE file_slices SET ciphertext_len = ?, blob_offset_start = ?, blob_offset_end = ? "
            "WHERE file_id = ? AND slice_number = ? AND recorded_by = ?",
            (offset_record['ciphertext_len'], offset_record['blob_offset_start'],
             offset_record['blob_offset_end'], file_id, offset_record['slice_number'], recorded_by)
        )

    log.info(f"consolidate_file_slices() consolidated {total_slices} slices "
             f"into {len(consolidated_blob):,} bytes for {file_id[:20]}...")

    return True


def get_file_data(file_id: str, recorded_by: str, db: Any) -> bytes | None:
    """Retrieve and decrypt file by file_id from message_attachment.

    Optimized path: If consolidated_blob exists, reads single BLOB (10-50x faster).
    Fallback path: Reads individual slices from file_slices table.

    Steps:
    1. Get attachment metadata (enc_key, root_hash, total_slices, consolidated_blob)
    2. If consolidated_blob exists, use fast path (single BLOB read + decrypt)
    3. Otherwise, use slow path (read all slices individually)
    4. Verify root_hash
    5. Return plaintext or None if verification fails

    Args:
        file_id: File ID to retrieve
        recorded_by: Peer requesting file (access control)
        db: Database connection

    Returns:
        File bytes, or None if incomplete/missing/invalid
    """
    log.debug(f"message_attachment.get_file_data() file_id={file_id[:20]}..., "
              f"recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get attachment with file metadata (including consolidated_blob)
    attachment_row = safedb.query_one(
        "SELECT blob_bytes, nonce_prefix, enc_key, root_hash, total_slices, consolidated_blob "
        "FROM message_attachments WHERE file_id = ? AND recorded_by = ? LIMIT 1",
        (file_id, recorded_by)
    )
    if not attachment_row:
        log.warning(f"message_attachment.get_file_data() attachment not found: {file_id[:20]}...")
        return None

    enc_key = attachment_row['enc_key']
    root_hash = attachment_row['root_hash']
    nonce_prefix = attachment_row['nonce_prefix']
    total_slices = attachment_row['total_slices']
    consolidated_blob = attachment_row['consolidated_blob']

    # FAST PATH: Use consolidated blob if available
    if consolidated_blob is not None:
        log.debug(f"get_file_data() using FAST PATH (consolidated blob) for {file_id[:20]}...")

        # Query slice offset information (set during consolidation)
        slice_rows = safedb.query_all(
            "SELECT slice_number, blob_offset_start, blob_offset_end, ciphertext_len FROM file_slices "
            "WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number ASC",
            (file_id, recorded_by)
        )

        # Verify we have offset information for all slices
        if len(slice_rows) != total_slices or not all(row.get('blob_offset_start') is not None for row in slice_rows):
            log.debug(f"get_file_data() FAST PATH: offset information not available, falling back to SLOW PATH")
            consolidated_blob = None
        else:
            plaintext_slices = []
            ciphertext_slices = []  # For root_hash verification

            try:
                for slice_row in slice_rows:
                    blob_offset_start = slice_row['blob_offset_start']
                    blob_offset_end = slice_row['blob_offset_end']
                    ciphertext_len = slice_row['ciphertext_len']

                    # Extract slice components from consolidated blob using deterministic offsets
                    # Format: nonce(12) + ciphertext(var) + poly_tag(16)
                    slice_blob = consolidated_blob[blob_offset_start:blob_offset_end]

                    nonce = slice_blob[0:12]
                    ciphertext = slice_blob[12:12+ciphertext_len]
                    poly_tag = slice_blob[12+ciphertext_len:12+ciphertext_len+16]

                    # Decrypt slice
                    plaintext = crypto.decrypt_file_slice(ciphertext, poly_tag, enc_key, nonce)
                    plaintext_slices.append(plaintext)
                    ciphertext_slices.append(ciphertext)

                # Fast path succeeded, verify root hash and return
                plaintext_full = b''.join(plaintext_slices)
                computed_root_hash = crypto.compute_root_hash(ciphertext_slices)

                if computed_root_hash != root_hash:
                    log.error(f"get_file_data() FAST PATH root_hash mismatch!")
                    return None

                plaintext_full = plaintext_full[:attachment_row['blob_bytes']]
                log.info(f"get_file_data() FAST PATH success: {file_id[:20]}..., size={len(plaintext_full)}B")
                return plaintext_full

            except Exception as e:
                log.error(f"get_file_data() FAST PATH decryption failed: {e}")
                log.info(f"get_file_data() falling back to SLOW PATH due to decryption error")
                consolidated_blob = None

    # SLOW PATH: Read individual slices from file_slices table
    log.debug(f"get_file_data() using SLOW PATH (individual slices) for {file_id[:20]}...")

    slice_rows = safedb.query_all(
        "SELECT slice_number, nonce, ciphertext, poly_tag FROM file_slices "
        "WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number ASC",
        (file_id, recorded_by)
    )

    if len(slice_rows) != total_slices:
        log.info(f"message_attachment.get_file_data() incomplete: have {len(slice_rows)}/{total_slices} slices, "
                 f"requesting sync from peers")

        # File slices will be synced via negentropy
        return None

    # Decrypt slices
    plaintext_slices = []
    for slice_row in slice_rows:
        ciphertext = slice_row['ciphertext']
        poly_tag = slice_row['poly_tag']
        nonce = slice_row['nonce']

        try:
            plaintext = crypto.decrypt_file_slice(ciphertext, poly_tag, enc_key, nonce)
            plaintext_slices.append(plaintext)
        except Exception as e:
            log.error(f"message_attachment.get_file_data() decryption failed: {e}")
            return None

    # Concatenate plaintext
    plaintext_full = b''.join(plaintext_slices)

    # Verify root_hash
    computed_root_hash = crypto.compute_root_hash(
        [slice_row['ciphertext'] for slice_row in slice_rows]
    )

    if computed_root_hash != root_hash:
        log.error(f"message_attachment.get_file_data() root_hash mismatch!")
        return None

    plaintext_full = plaintext_full[:attachment_row['blob_bytes']]
    log.info(f"message_attachment.get_file_data() successfully retrieved {file_id[:20]}..., "
             f"size={len(plaintext_full)}B")
    return plaintext_full


def list_attachments(recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """Return attachment rows with message content for display."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_all(
        """SELECT ma.file_id, ma.filename, ma.mime_type, ma.blob_bytes, ma.total_slices,
                  m.content as message_content
           FROM message_attachments ma
           JOIN messages m ON ma.message_id = m.message_id AND m.recorded_by = ma.recorded_by
           WHERE ma.recorded_by = ?
           ORDER BY ma.recorded_at DESC""",
        (recorded_by,),
    )


def get_file_download_progress(file_id: str, recorded_by: str, db: Any,
                               prev_progress: dict[str, Any] | None = None,
                               elapsed_ms: int | None = None) -> dict[str, Any] | None:
    """Get download progress for a file attachment.

    Returns progress information for UI/frontend display:
    - slices_received: Number of slices downloaded so far
    - total_slices: Total slices in the file
    - bytes_received: Actual bytes of ciphertext received (sum of all slice ciphertext lengths)
    - percentage_complete: 0-100 (int)
    - is_complete: Boolean (all slices received)
    - filename: Original filename
    - size_bytes: Total file size
    - size_human: Human-readable size (e.g., "1.2 MB")
    - speed_bytes_per_sec: Download speed in bytes/second (requires elapsed_ms)
    - speed_human: Human-readable speed (e.g., "1.2 MB/s")
    - eta_seconds: Estimated seconds to complete (requires elapsed_ms)

    For calculating speed:
    1. Call progress = get_file_download_progress(file_id, peer_id, db)
    2. Wait some time (e.g., 100ms or 1 second)
    3. Call progress = get_file_download_progress(file_id, peer_id, db,
                                                   prev_progress=progress,
                                                   elapsed_ms=time_waited_in_ms)

    This enables progress displays like:
    "Downloading file.pdf (3 of 5 slices, 60% complete) - 2.3 MB/s, ETA 2s"

    Args:
        file_id: File ID to check progress
        recorded_by: Peer requesting progress (access control)
        db: Database connection
        prev_progress: Previous progress dict (for speed calculation)
        elapsed_ms: Milliseconds elapsed since prev_progress (for speed calculation)

    Returns:
        Progress dict, or None if attachment not found

    Example:
        progress = get_file_download_progress(file_id, peer_id, db)
        if progress:
            print(f"Downloading {progress['filename']}: "
                  f"{progress['slices_received']}/{progress['total_slices']} slices "
                  f"({progress['percentage_complete']}%)")

        # To show speed and ETA:
        time.sleep(0.1)
        progress = get_file_download_progress(file_id, peer_id, db,
                                             prev_progress=progress,
                                             elapsed_ms=100)
        if progress:
            print(f"{progress['filename']}: {progress['percentage_complete']}% "
                  f"({progress['speed_human']}, ETA {progress['eta_seconds']}s)")
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get attachment metadata
    attachment_row = safedb.query_one(
        "SELECT filename, blob_bytes, total_slices "
        "FROM message_attachments WHERE file_id = ? AND recorded_by = ? LIMIT 1",
        (file_id, recorded_by)
    )
    if not attachment_row:
        return None

    total_slices = attachment_row['total_slices']
    size_bytes = attachment_row['blob_bytes']
    filename = attachment_row['filename'] or 'untitled'

    # Count received slices and sum actual bytes received
    slice_rows = safedb.query_all(
        "SELECT COUNT(*) as count, SUM(LENGTH(ciphertext)) as bytes_received FROM file_slices "
        "WHERE file_id = ? AND recorded_by = ?",
        (file_id, recorded_by)
    )
    slices_received = slice_rows[0]['count'] if slice_rows else 0
    bytes_received = slice_rows[0]['bytes_received'] if slice_rows and slice_rows[0]['bytes_received'] else 0
    if bytes_received > size_bytes:
        bytes_received = size_bytes

    # Calculate percentage
    if total_slices > 0:
        percentage_complete = int((slices_received / total_slices) * 100)
    else:
        percentage_complete = 0

    is_complete = (slices_received == total_slices)

    # Auto-consolidate when download completes
    if is_complete:
        # Check if already consolidated
        existing_consolidated = safedb.query_one(
            "SELECT consolidated_blob FROM message_attachments "
            "WHERE file_id = ? AND recorded_by = ?",
            (file_id, recorded_by)
        )
        if existing_consolidated and existing_consolidated['consolidated_blob'] is None:
            # Not yet consolidated, do it now
            log.info(f"get_file_download_progress() download complete, consolidating {file_id[:20]}...")
            consolidate_file_slices(file_id, recorded_by, db)

    # Human-readable size
    size_human = _format_bytes(size_bytes)

    # Calculate speed and ETA if we have previous progress
    speed_bytes_per_sec = 0
    speed_human = "0 B/s"
    eta_seconds = None

    if prev_progress is not None and elapsed_ms is not None and elapsed_ms > 0:
        # Calculate bytes transferred using actual received bytes
        prev_bytes_received = prev_progress.get('bytes_received', 0)
        bytes_transferred = bytes_received - prev_bytes_received

        # Calculate speed in bytes/second
        elapsed_seconds = elapsed_ms / 1000.0
        if elapsed_seconds > 0:
            speed_bytes_per_sec = int(bytes_transferred / elapsed_seconds)
            speed_human = _format_bytes(speed_bytes_per_sec) + "/s"

            # Calculate ETA based on actual bytes remaining
            if speed_bytes_per_sec > 0 and not is_complete:
                remaining_bytes = size_bytes - bytes_received
                eta_seconds = int(remaining_bytes / speed_bytes_per_sec)

    log.debug(f"get_file_download_progress() file_id={file_id[:20]}..., "
              f"progress={slices_received}/{total_slices} ({percentage_complete}%), "
              f"speed={speed_human}")

    result = {
        'file_id': file_id,
        'filename': filename,
        'slices_received': slices_received,
        'total_slices': total_slices,
        'bytes_received': bytes_received,
        'percentage_complete': percentage_complete,
        'is_complete': is_complete,
        'size_bytes': size_bytes,
        'size_human': size_human,
        'speed_bytes_per_sec': speed_bytes_per_sec,
        'speed_human': speed_human,
    }

    if eta_seconds is not None:
        result['eta_seconds'] = eta_seconds

    return result


def get_file_as_data_uri(file_id: str, recorded_by: str, db: Any,
                         include_metadata: bool = False) -> str | dict[str, Any] | None:
    """Get file as data URI for frontend use.

    Returns a data URI string suitable for embedding in HTML/frontend:
        data:image/png;base64,iVBORw0KGgoAAAANS...

    This is useful for:
    - Displaying images: <img src="data:image/png;base64,...">
    - Embedding files in HTML
    - Sending files to frontend without separate HTTP requests

    Args:
        file_id: File ID to retrieve
        recorded_by: Peer requesting file (access control)
        db: Database connection
        include_metadata: If True, return dict with data_uri and metadata

    Returns:
        If include_metadata=False: data URI string, or None if file unavailable
        If include_metadata=True: dict with {
            'data_uri': str,
            'filename': str,
            'mime_type': str,
            'size_bytes': int,
            'size_human': str
        }, or None if file unavailable

    Example:
        # Simple usage
        data_uri = get_file_as_data_uri(file_id, peer_id, db)
        # Returns: "data:image/png;base64,iVBORw0KGgo..."

        # With metadata
        result = get_file_as_data_uri(file_id, peer_id, db, include_metadata=True)
        # Returns: {
        #     'data_uri': 'data:image/png;base64,...',
        #     'filename': 'photo.png',
        #     'mime_type': 'image/png',
        #     'size_bytes': 12345,
        #     'size_human': '12.1 KB'
        # }
    """
    log.debug(f"message_attachment.get_file_as_data_uri() file_id={file_id[:20]}..., "
              f"recorded_by={recorded_by[:20]}...")

    # Get file data
    file_data = get_file_data(file_id, recorded_by, db)
    if file_data is None:
        log.debug(f"get_file_as_data_uri() file not available: {file_id[:20]}...")
        return None

    # Get metadata
    safedb = create_safe_db(db, recorded_by=recorded_by)
    attachment_row = safedb.query_one(
        "SELECT filename, mime_type, blob_bytes "
        "FROM message_attachments WHERE file_id = ? AND recorded_by = ? LIMIT 1",
        (file_id, recorded_by)
    )
    if not attachment_row:
        log.warning(f"get_file_as_data_uri() metadata not found: {file_id[:20]}...")
        return None

    filename = attachment_row['filename'] or 'untitled'
    mime_type = attachment_row['mime_type'] or 'application/octet-stream'
    size_bytes = attachment_row['blob_bytes']

    # Encode to base64
    base64_data = base64.b64encode(file_data).decode('ascii')

    # Create data URI
    data_uri = f"data:{mime_type};base64,{base64_data}"

    log.info(f"get_file_as_data_uri() created data URI for {file_id[:20]}..., "
             f"size={size_bytes}B, mime={mime_type}")

    if include_metadata:
        return {
            'data_uri': data_uri,
            'filename': filename,
            'mime_type': mime_type,
            'size_bytes': size_bytes,
            'size_human': _format_bytes(size_bytes)
        }
    else:
        return data_uri


def _format_bytes(num_bytes: int) -> str:
    """Convert bytes to human-readable format.

    Args:
        num_bytes: Number of bytes

    Returns:
        String like "1.2 MB" or "45 KB"
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024:
            if unit == 'B':
                return f"{num_bytes} {unit}"
            else:
                return f"{num_bytes / 1024:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes / 1024:.1f} PB"
