"""File event type - metadata for encrypted files with message attachments.

File descriptor IS group-wrapped (access control via encryption key).
File slices are NOT group-wrapped (stored with their own symmetric encryption).
"""
from typing import Any
import logging
import crypto
import store
from events.content import file_slice, message_attachment
from events.group import group
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)

SLICE_SIZE = 450  # bytes - matches ideal protocol design


def create_with_attachment(peer_id: str, message_id: str, file_data: bytes,
                          filename: str | None, mime_type: str | None,
                          t_ms: int, db: Any) -> dict[str, Any]:
    """Create file from bytes and attach to message. Complete transaction.

    Steps:
    1. Generate enc_key, nonce_prefix
    2. Split file_data into 450-byte chunks
    3. Encrypt each chunk → create file_slice events
    4. Compute file_id (BLAKE2b-128 of full ciphertext)
    5. Compute root_hash (BLAKE2b-256 of concatenated slices)
    6. Create file descriptor event (GROUP-ENCRYPTED)
    7. Create message_attachment event (GROUP-ENCRYPTED)

    Args:
        peer_id: Local peer creating the file
        message_id: Message to attach file to
        file_data: Raw file bytes
        filename: Optional filename
        mime_type: Optional MIME type
        t_ms: Timestamp
        db: Database connection

    Returns:
        {
            'file_id': file_id,
            'slice_count': number_of_slices,
            'blob_bytes': len(file_data)
        }
    """
    log.info(f"file.create_with_attachment() message_id={message_id[:20]}..., "
             f"file_size={len(file_data)}B")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get message to verify channel_id
    message_row = safedb.query_one(
        "SELECT group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, peer_id)
    )
    if not message_row:
        raise ValueError(f"Message {message_id} not found for peer {peer_id}")

    group_id = message_row['group_id']

    # Get peer_shared_id for created_by field
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set")

    peer_shared_id = peer_self_row['peer_shared_id']

    # Step 1-2: Generate encryption key and nonce prefix
    enc_key = crypto.generate_secret()  # 32 bytes
    nonce_prefix = crypto.generate_secret()[:20]  # Use first 20 bytes

    # Step 3: Split file into slices and encrypt
    slice_ciphertexts = []  # For computing root_hash and file_id
    slices_to_create = []  # Store for creation after computing file_id

    for slice_number in range(0, len(file_data), SLICE_SIZE):
        plaintext_slice = file_data[slice_number:slice_number + SLICE_SIZE]

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

    # Step 5: Create file_slice events (now that we have file_id)
    slice_count = 0
    for slice_number, slice_nonce, ciphertext, poly_tag in slices_to_create:
        file_slice.create(
            file_id=file_id,  # Now we have the correct file_id
            slice_number=slice_number,
            nonce=slice_nonce,
            ciphertext=ciphertext,
            poly_tag=poly_tag,
            peer_id=peer_id,
            created_by=peer_shared_id,
            t_ms=t_ms,
            db=db
        )
        slice_count += 1

    log.info(f"file.create_with_attachment() created {slice_count} slices, "
             f"file_id={file_id[:20]}...")

    # Step 6: Compute root_hash
    root_hash = crypto.compute_root_hash(slice_ciphertexts)

    # Step 7: Create file descriptor event (GROUP-ENCRYPTED)
    # Build event structure
    event_data = {
        'type': 'file',
        'file_id': file_id,
        'blob_bytes': len(file_data),
        'nonce_prefix': crypto.b64encode(nonce_prefix),
        'enc_key': crypto.b64encode(enc_key),
        'root_hash': crypto.b64encode(root_hash),
        'total_slices': slice_count,
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign the event
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get group key for encryption
    key_data = group.pick_key(group_id, peer_id, db)

    # Wrap (canonicalize + group encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event
    file_event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"file.create_with_attachment() created file event_id={file_event_id[:20]}...")

    # Step 8: Create message_attachment event (GROUP-ENCRYPTED)
    message_attachment.create(
        peer_id=peer_id,
        message_id=message_id,
        file_id=file_id,
        filename=filename,
        mime_type=mime_type,
        t_ms=t_ms,
        db=db
    )

    return {
        'file_id': file_id,
        'slice_count': slice_count,
        'blob_bytes': len(file_data)
    }


def project(event_id: str, event_data: dict[str, Any], recorded_by: str,
            recorded_at: int, db: Any) -> None:
    """Project file descriptor event into files table.

    Args:
        event_id: Event ID
        event_data: Decrypted/unwrapped event data
        recorded_by: Peer who recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection
    """
    log.debug(f"file.project() event_id={event_id[:20]}..., recorded_by={recorded_by[:20]}...")

    file_id = event_data.get('file_id')
    blob_bytes = event_data.get('blob_bytes')
    nonce_prefix_b64 = event_data.get('nonce_prefix')
    enc_key_b64 = event_data.get('enc_key')
    root_hash_b64 = event_data.get('root_hash')
    total_slices = event_data.get('total_slices')

    if not all([file_id, blob_bytes is not None, nonce_prefix_b64,
                enc_key_b64, root_hash_b64, total_slices is not None]):
        log.warning(f"file.project() missing fields in event_data: {list(event_data.keys())}")
        return

    # Decode from base64
    nonce_prefix = crypto.b64decode(nonce_prefix_b64)
    enc_key = crypto.b64decode(enc_key_b64)
    root_hash = crypto.b64decode(root_hash_b64)

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Insert or replace into files table
    safedb.execute(
        """INSERT OR REPLACE INTO files
           (file_id, blob_bytes, nonce_prefix, enc_key, root_hash, total_slices,
            recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (file_id, blob_bytes, nonce_prefix, enc_key, root_hash, total_slices,
         recorded_by, recorded_at)
    )

    log.debug(f"file.project() projected file {file_id[:20]}..., slices={total_slices}")


def get_file(file_id: str, recorded_by: str, db: Any) -> bytes | None:
    """Retrieve and decrypt file by ID.

    Steps:
    1. Get file metadata (enc_key, root_hash, total_slices)
    2. Get all slices from file_slices table
    3. If incomplete, return None
    4. Decrypt each slice with enc_key
    5. Concatenate plaintext slices
    6. Verify root_hash
    7. Return plaintext or None if verification fails

    Args:
        file_id: File ID to retrieve
        recorded_by: Peer requesting file (access control)
        db: Database connection

    Returns:
        File bytes, or None if incomplete/missing/invalid
    """
    log.debug(f"file.get_file() file_id={file_id[:20]}..., recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get file metadata
    file_row = safedb.query_one(
        "SELECT blob_bytes, nonce_prefix, enc_key, root_hash, total_slices "
        "FROM files WHERE file_id = ? AND recorded_by = ? LIMIT 1",
        (file_id, recorded_by)
    )
    if not file_row:
        log.warning(f"file.get_file() file not found: {file_id[:20]}...")
        return None

    enc_key = file_row['enc_key']
    root_hash = file_row['root_hash']
    nonce_prefix = file_row['nonce_prefix']
    total_slices = file_row['total_slices']

    # Get all slices
    slice_rows = safedb.query_all(
        "SELECT slice_number, nonce, ciphertext, poly_tag FROM file_slices "
        "WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number ASC",
        (file_id, recorded_by)
    )

    if len(slice_rows) != total_slices:
        log.warning(f"file.get_file() incomplete: have {len(slice_rows)}/{total_slices} slices")
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
            log.error(f"file.get_file() decryption failed: {e}")
            return None

    # Concatenate plaintext
    plaintext_full = b''.join(plaintext_slices)

    # Verify root_hash
    computed_root_hash = crypto.compute_root_hash(
        [slice_row['ciphertext'] for slice_row in slice_rows]
    )

    if computed_root_hash != root_hash:
        log.error(f"file.get_file() root_hash mismatch!")
        return None

    log.info(f"file.get_file() successfully retrieved {file_id[:20]}..., "
             f"size={len(plaintext_full)}B")
    return plaintext_full
