"""Message attachment event type - attaches files to messages with encryption metadata.

Attachments ARE group-wrapped (access control).
File descriptor data (enc_key, root_hash, etc.) is now embedded in this event
instead of a separate 'file' event.
"""
from typing import Any
import base64
import io
import logging
from PIL import Image
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
        log.info(f"message_attachment.get_file_data() incomplete: have {len(slice_rows)}/{total_slices} slices, "
                 f"requesting sync from peers")

        # Auto-trigger file sync with high priority (TTL = 0 means forever)
        try:
            from events.transit import sync_file
            sync_file.request_file_sync(file_id, recorded_by, priority=10, ttl_ms=0, t_ms=0, db=db)
        except Exception as e:
            log.warning(f"message_attachment.get_file_data() failed to request sync: {e}")

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

    # Calculate percentage
    if total_slices > 0:
        percentage_complete = int((slices_received / total_slices) * 100)
    else:
        percentage_complete = 0

    is_complete = (slices_received == total_slices)

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
