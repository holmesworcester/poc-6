"""Message attachment event type - attaches files to messages with encryption metadata.

Attachments ARE group-wrapped (access control).
File descriptor data (enc_key, root_hash, etc.) is now embedded in this event
instead of a separate 'file' event.
"""
from typing import Any
import logging
import crypto
import store
from events.group import group
from events.identity import peer
from events.content import file_slice
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)

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
    log.info(f"message_attachment.create() message_id={message_id[:20]}..., "
             f"file_size={len(file_data)}B")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get message to verify access and get group_id
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

    log.info(f"message_attachment.create() created {slice_count} slices, "
             f"file_id={file_id[:20]}...")

    # Step 6: Compute root_hash
    root_hash = crypto.compute_root_hash(slice_ciphertexts)

    # Step 7: Build event structure with all file descriptor fields
    event_data = {
        'type': 'message_attachment',
        'message_id': message_id,
        'file_id': file_id,
        'filename': filename,
        'mime_type': mime_type,
        # File descriptor fields (was in separate 'file' event)
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
    attachment_event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"message_attachment.create() created attachment_event_id={attachment_event_id[:20]}...")

    return {
        'file_id': file_id,
        'slice_count': slice_count,
        'blob_bytes': len(file_data),
        'attachment_event_id': attachment_event_id
    }


def project(event_id: str, event_data: dict[str, Any], recorded_by: str,
            recorded_at: int, db: Any) -> None:
    """Project message_attachment event into message_attachments table.

    Now includes file descriptor fields (enc_key, root_hash, etc.)
    VALIDATION: Only the message creator can attach files to their message.

    Args:
        event_id: Event ID
        event_data: Decrypted/unwrapped event data
        recorded_by: Peer who recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection
    """
    log.debug(f"message_attachment.project() event_id={event_id[:20]}..., "
              f"recorded_by={recorded_by[:20]}...")

    message_id = event_data.get('message_id')
    file_id = event_data.get('file_id')
    filename = event_data.get('filename')
    mime_type = event_data.get('mime_type')
    created_by = event_data.get('created_by')

    # File descriptor fields (now in this event)
    blob_bytes = event_data.get('blob_bytes')
    nonce_prefix_b64 = event_data.get('nonce_prefix')
    enc_key_b64 = event_data.get('enc_key')
    root_hash_b64 = event_data.get('root_hash')
    total_slices = event_data.get('total_slices')

    if not all([message_id, file_id, created_by, blob_bytes is not None,
                nonce_prefix_b64, enc_key_b64, root_hash_b64, total_slices is not None]):
        log.warning(f"message_attachment.project() missing required fields: {list(event_data.keys())}")
        return

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Validate: attachment creator must match message creator
    # NOTE: message_id is a dependency, so the message should already be projected
    message_row = safedb.query_one(
        "SELECT author_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, recorded_by)
    )

    if not message_row:
        log.error(f"message_attachment.project() BUG: message not found (should be blocked by deps): {message_id[:20]}...")
        return

    if message_row['author_id'] != created_by:
        log.warning(f"message_attachment.project() VALIDATION FAILED: attachment created_by={created_by[:20]}... "
                   f"does not match message author_id={message_row['author_id'][:20]}...")
        return

    # Decode file descriptor fields
    nonce_prefix = crypto.b64decode(nonce_prefix_b64)
    enc_key = crypto.b64decode(enc_key_b64)
    root_hash = crypto.b64decode(root_hash_b64)

    # Insert or ignore (now includes file descriptor fields)
    safedb.execute(
        """INSERT OR IGNORE INTO message_attachments
           (message_id, file_id, filename, mime_type, blob_bytes, nonce_prefix,
            enc_key, root_hash, total_slices, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (message_id, file_id, filename, mime_type, blob_bytes, nonce_prefix,
         enc_key, root_hash, total_slices, recorded_by, recorded_at)
    )

    # Record dependency for cascading deletion
    # attachment depends on message (attachment is a child of message)
    safedb.execute(
        """INSERT OR IGNORE INTO event_dependencies
           (child_event_id, parent_event_id, recorded_by, dependency_type)
           VALUES (?, ?, ?, ?)""",
        (event_id, message_id, recorded_by, 'message')
    )

    log.debug(f"message_attachment.project() projected attachment "
              f"message={message_id[:20]}... file={file_id[:20]}... slices={total_slices}")


def get_file_data(file_id: str, recorded_by: str, db: Any) -> bytes | None:
    """Retrieve and decrypt file by file_id from message_attachment.

    Steps:
    1. Get attachment metadata (enc_key, root_hash, total_slices)
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
    log.debug(f"message_attachment.get_file_data() file_id={file_id[:20]}..., "
              f"recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get attachment with file metadata
    attachment_row = safedb.query_one(
        "SELECT blob_bytes, nonce_prefix, enc_key, root_hash, total_slices "
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

    # Get all slices
    slice_rows = safedb.query_all(
        "SELECT slice_number, nonce, ciphertext, poly_tag FROM file_slices "
        "WHERE file_id = ? AND recorded_by = ? ORDER BY slice_number ASC",
        (file_id, recorded_by)
    )

    if len(slice_rows) != total_slices:
        log.warning(f"message_attachment.get_file_data() incomplete: have {len(slice_rows)}/{total_slices} slices")
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

    log.info(f"message_attachment.get_file_data() successfully retrieved {file_id[:20]}..., "
             f"size={len(plaintext_full)}B")
    return plaintext_full
