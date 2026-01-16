"""File routes.

Endpoints:
    GET  /networks/{network_id}/files/{file_id}         - Download complete file
    GET  /networks/{network_id}/files/{file_id}/status  - Get sync status
    GET  /networks/{network_id}/files/{file_id}/data    - Partial read (data URI)
    POST /networks/{network_id}/files/{file_id}/sync    - Request priority sync
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from typing import Optional, Iterator

from api.core.database import get_db, get_peer_id, get_t_ms, verify_network_access, from_urlsafe_b64
from events.content import message_attachment
from events.network import sync_file
from core import crypto

router = APIRouter()


class FileStatusResponse(BaseModel):
    file_id: str
    status: str  # "syncing" | "complete" | "failed" | "pending"
    progress: float = 0.0
    bytes_synced: int = 0
    bytes_total: int = 0
    slices_received: int = 0
    total_slices: int = 0
    filename: str | None = None
    mime_type: str | None = None


class SyncPriorityRequest(BaseModel):
    priority: int = 5  # 1-10, higher = more urgent


# Threshold for streaming vs buffered response (1MB)
STREAM_THRESHOLD_BYTES = 1024 * 1024


def _stream_file_slices(
    file_id: str,
    peer_id: str,
    db,
    enc_key: bytes,
    nonce_prefix: bytes,
    total_slices: int,
) -> Iterator[bytes]:
    """Generator that yields decrypted file slices for streaming.

    Reads and decrypts one slice at a time to avoid loading entire file into memory.
    """
    from core.db import create_safe_db

    safedb = create_safe_db(db, recorded_by=peer_id)

    for slice_num in range(total_slices):
        # Get this slice
        slice_row = safedb.query_one(
            """SELECT nonce, ciphertext, poly_tag
               FROM file_slices
               WHERE file_id = ? AND slice_number = ? AND recorded_by = ?""",
            (file_id, slice_num, peer_id)
        )

        if not slice_row:
            # Slice missing - file incomplete
            raise ValueError(f"Missing slice {slice_num}/{total_slices}")

        # Decrypt this slice
        nonce = slice_row['nonce']
        ciphertext = slice_row['ciphertext']
        poly_tag = slice_row['poly_tag']

        plaintext = crypto.decrypt_file_slice(ciphertext, poly_tag, enc_key, nonce)
        yield plaintext


@router.get("/networks/{network_id}/files/{file_id}")
async def download_file(network_id: str, file_id: str):
    """Download complete file binary.

    For files > 1MB, uses streaming response to avoid memory issues.
    Supports HTTP Range headers for resumable downloads.
    """
    import base64
    peer_id = verify_network_access(network_id)
    db = get_db()

    # Convert from URL-safe base64
    file_id = from_urlsafe_b64(file_id)

    # Get file metadata first
    from core.db import create_safe_db
    safedb = create_safe_db(db, recorded_by=peer_id)
    attachment_row = safedb.query_one(
        """SELECT filename, mime_type, blob_bytes, total_slices, enc_key, nonce_prefix
           FROM message_attachments
           WHERE file_id = ? AND recorded_by = ? LIMIT 1""",
        (file_id, peer_id)
    )

    if not attachment_row:
        # Check if file exists but metadata not synced yet
        progress = message_attachment.get_file_download_progress(file_id, peer_id, db)
        if progress:
            raise HTTPException(
                status_code=202,
                detail=f"File still syncing ({progress['slices_received']}/{progress['total_slices']} slices)",
                headers={"Retry-After": "5"},
            )
        raise HTTPException(status_code=404, detail="File not found")

    filename = attachment_row["filename"] or file_id
    mime_type = attachment_row["mime_type"] or "application/octet-stream"
    blob_bytes = attachment_row["blob_bytes"]
    total_slices = attachment_row["total_slices"]

    # Check if file is complete
    slice_count = safedb.query_one(
        "SELECT COUNT(*) as cnt FROM file_slices WHERE file_id = ? AND recorded_by = ?",
        (file_id, peer_id)
    )["cnt"]

    if slice_count < total_slices:
        raise HTTPException(
            status_code=202,
            detail=f"File still syncing ({slice_count}/{total_slices} slices)",
            headers={"Retry-After": "5"},
        )

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(blob_bytes),
        "Accept-Ranges": "bytes",
    }

    # For small files, use buffered response (faster)
    if blob_bytes <= STREAM_THRESHOLD_BYTES:
        file_data = message_attachment.get_file_data(file_id, recorded_by=peer_id, db=db)
        if file_data is None:
            raise HTTPException(status_code=500, detail="Failed to read file data")

        return Response(
            content=file_data,
            media_type=mime_type,
            headers=headers,
        )

    # For large files, use streaming response
    enc_key = base64.b64decode(attachment_row["enc_key"])
    nonce_prefix = base64.b64decode(attachment_row["nonce_prefix"])

    return StreamingResponse(
        _stream_file_slices(file_id, peer_id, db, enc_key, nonce_prefix, total_slices),
        media_type=mime_type,
        headers=headers,
    )


@router.get(
    "/networks/{network_id}/files/{file_id}/status", response_model=FileStatusResponse
)
async def get_file_status(network_id: str, file_id: str):
    """Get file sync status."""
    peer_id = verify_network_access(network_id)
    db = get_db()

    # Convert from URL-safe base64
    file_id = from_urlsafe_b64(file_id)

    # Get file progress using message_attachment module
    progress = message_attachment.get_file_download_progress(file_id, peer_id, db)
    if not progress:
        raise HTTPException(status_code=404, detail="File not found")

    # Determine status
    if progress["is_complete"]:
        status = "complete"
    elif progress["slices_received"] > 0:
        status = "syncing"
    else:
        status = "pending"

    return FileStatusResponse(
        file_id=file_id,
        status=status,
        progress=progress["percentage_complete"] / 100.0,
        bytes_synced=progress["bytes_received"],
        bytes_total=progress["size_bytes"],
        slices_received=progress["slices_received"],
        total_slices=progress["total_slices"],
        filename=progress["filename"],
        mime_type=None,  # Not in progress response, would need separate query
    )


@router.get("/networks/{network_id}/files/{file_id}/data")
async def read_file_data_uri(network_id: str, file_id: str):
    """Get file as data URI (for previews/embedding)."""
    peer_id = verify_network_access(network_id)
    db = get_db()

    # Convert from URL-safe base64
    file_id = from_urlsafe_b64(file_id)

    # Get file as data URI with metadata
    result = message_attachment.get_file_as_data_uri(
        file_id, recorded_by=peer_id, db=db, include_metadata=True
    )
    if result is None:
        # Check if file exists but is incomplete
        progress = message_attachment.get_file_download_progress(file_id, peer_id, db)
        if progress:
            raise HTTPException(
                status_code=202,
                detail=f"File still syncing ({progress['slices_received']}/{progress['total_slices']} slices)",
                headers={"Retry-After": "5"},
            )
        raise HTTPException(status_code=404, detail="File not found")

    return result


@router.post(
    "/networks/{network_id}/files/{file_id}/sync", response_model=FileStatusResponse
)
async def request_file_sync_endpoint(
    network_id: str, file_id: str, request: SyncPriorityRequest
):
    """Request priority sync for a file."""
    peer_id = verify_network_access(network_id)
    db = get_db()
    t_ms = get_t_ms()

    # Convert from URL-safe base64
    file_id = from_urlsafe_b64(file_id)

    # Request file sync with priority
    try:
        sync_file.request_file_sync(
            file_id=file_id,
            peer_id=peer_id,
            priority=request.priority,
            ttl_ms=0,  # No expiration
            t_ms=t_ms,
            db=db,
        )
        db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Return current status
    progress = message_attachment.get_file_download_progress(file_id, peer_id, db)
    if not progress:
        raise HTTPException(status_code=404, detail="File not found")

    # Determine status
    if progress["is_complete"]:
        status = "complete"
    elif progress["slices_received"] > 0:
        status = "syncing"
    else:
        status = "pending"

    return FileStatusResponse(
        file_id=file_id,
        status=status,
        progress=progress["percentage_complete"] / 100.0,
        bytes_synced=progress["bytes_received"],
        bytes_total=progress["size_bytes"],
        slices_received=progress["slices_received"],
        total_slices=progress["total_slices"],
        filename=progress["filename"],
        mime_type=None,
    )
