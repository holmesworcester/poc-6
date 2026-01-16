"""Message routes.

Endpoints:
    GET    /networks/{network_id}/channels/{channel_id}/messages - List messages
    POST   /networks/{network_id}/channels/{channel_id}/messages - Send message
    PATCH  /networks/{network_id}/messages/{message_id}          - Edit message
    DELETE /networks/{network_id}/messages/{message_id}          - Delete message
"""

import tempfile
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Request
from pydantic import BaseModel
from typing import Optional

from api.core.database import get_db, get_peer_id, get_t_ms, verify_network_access, from_urlsafe_b64
from events.content import message, message_deletion, message_update, message_attachment
from events.content import channel as channel_module

# Chunk size for reading uploads (64KB)
UPLOAD_CHUNK_SIZE = 64 * 1024

router = APIRouter()


class AttachmentResponse(BaseModel):
    file_id: str
    filename: str | None = None
    mime_type: str | None = None
    blob_bytes: int | None = None


class ReactionResponse(BaseModel):
    emoji: str
    count: int
    user_ids: list[str]


class MessageResponse(BaseModel):
    message_id: str
    channel_id: str
    author_id: str
    author_name: str | None = None
    content: str
    created_at: int
    edited_at: int | None = None
    attachments: list[AttachmentResponse] = []
    reactions: list[ReactionResponse] = []


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    cursors: dict[str, str | None]


class SendMessageRequest(BaseModel):
    text: str


class SendMessageResponse(BaseModel):
    message_id: str
    attachments: list[AttachmentResponse] = []


class EditMessageRequest(BaseModel):
    text: str


def _get_paginated_messages(
    channel_id: str,
    peer_id: str,
    db,
    cursor: str | None,
    direction: str,
    limit: int,
) -> list[dict]:
    """Get paginated messages with cursor support.

    Args:
        cursor: Message ID to start from (URL-safe base64 converted)
        direction: 'older' for messages before cursor, 'newer' for after
        limit: Max messages to return
    """
    from core.db import create_safe_db
    from events.content import message_reaction, message_update

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Build query based on cursor and direction
    if cursor:
        cursor = from_urlsafe_b64(cursor)
        # Get the timestamp of the cursor message
        cursor_msg = safedb.query_one(
            "SELECT created_at FROM messages WHERE message_id = ? AND recorded_by = ?",
            (cursor, peer_id)
        )
        if not cursor_msg:
            # Invalid cursor, return from beginning/end
            cursor = None

    if cursor and cursor_msg:
        cursor_time = cursor_msg["created_at"]
        if direction == "older":
            # Messages older than cursor (before), newest first
            messages = safedb.query(
                """SELECT m.*, un.name as author_name
                   FROM messages m
                   LEFT JOIN user_names un ON m.author_id = un.user_id AND m.recorded_by = un.recorded_by
                   WHERE m.channel_id = ? AND m.recorded_by = ?
                     AND (m.created_at < ? OR (m.created_at = ? AND m.message_id < ?))
                   ORDER BY m.created_at DESC, m.message_id DESC
                   LIMIT ?""",
                (channel_id, peer_id, cursor_time, cursor_time, cursor, limit)
            )
            # Reverse to get chronological order
            messages = list(reversed(messages))
        else:
            # Messages newer than cursor (after), oldest first
            messages = safedb.query(
                """SELECT m.*, un.name as author_name
                   FROM messages m
                   LEFT JOIN user_names un ON m.author_id = un.user_id AND m.recorded_by = un.recorded_by
                   WHERE m.channel_id = ? AND m.recorded_by = ?
                     AND (m.created_at > ? OR (m.created_at = ? AND m.message_id > ?))
                   ORDER BY m.created_at ASC, m.message_id ASC
                   LIMIT ?""",
                (channel_id, peer_id, cursor_time, cursor_time, cursor, limit)
            )
    else:
        # No cursor - get most recent messages
        messages = safedb.query(
            """SELECT m.*, un.name as author_name
               FROM messages m
               LEFT JOIN user_names un ON m.author_id = un.user_id AND m.recorded_by = un.recorded_by
               WHERE m.channel_id = ? AND m.recorded_by = ?
               ORDER BY m.created_at DESC, m.message_id DESC
               LIMIT ?""",
            (channel_id, peer_id, limit)
        )
        # Reverse to get chronological order
        messages = list(reversed(messages))

    # Enrich with attachments and reactions
    for msg in messages:
        # Get attachments
        attachments = safedb.query(
            """SELECT file_id, filename, mime_type, blob_bytes
               FROM message_attachments
               WHERE message_id = ? AND recorded_by = ?""",
            (msg["message_id"], peer_id)
        )
        msg["attachments"] = list(attachments)

        # Get reactions
        reactions = safedb.query(
            """SELECT emoji, reactor_id
               FROM message_reactions
               WHERE message_id = ? AND recorded_by = ?""",
            (msg["message_id"], peer_id)
        )
        msg["reactions"] = list(reactions)

        # Apply message updates if any
        update = message_update.get(msg["message_id"], peer_id, db)
        if update:
            msg["content"] = update["new_content"]
            msg["edited_at"] = update["created_at"]

    return messages


@router.get(
    "/networks/{network_id}/channels/{channel_id}/messages",
    response_model=MessageListResponse,
)
async def list_messages(
    network_id: str,
    channel_id: str,
    cursor: Optional[str] = None,
    direction: str = "older",
    limit: int = 50,
):
    """List messages in a channel with cursor-based pagination.

    Args:
        cursor: Message ID to paginate from (URL-safe base64)
        direction: 'older' for messages before cursor, 'newer' for after
        limit: Max messages to return (default 50)

    Returns messages in chronological order (oldest first).
    """
    peer_id = verify_network_access(network_id)
    db = get_db()

    # Convert from URL-safe base64
    channel_id = from_urlsafe_b64(channel_id)

    # Verify channel exists
    ch = channel_module.get_by_id(channel_id, recorded_by=peer_id, db=db)
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Get paginated messages
    messages = _get_paginated_messages(
        channel_id=channel_id,
        peer_id=peer_id,
        db=db,
        cursor=cursor,
        direction=direction,
        limit=limit,
    )

    items = []
    for msg in messages:
        # Build attachment list
        attachments = []
        if msg.get("attachments"):
            for att in msg["attachments"]:
                attachments.append(
                    AttachmentResponse(
                        file_id=att.get("file_id", ""),
                        filename=att.get("filename"),
                        mime_type=att.get("mime_type"),
                        blob_bytes=att.get("blob_bytes"),
                    )
                )

        # Build reaction list
        reactions = []
        if msg.get("reactions"):
            # Group reactions by emoji
            emoji_counts: dict[str, dict] = {}
            for rxn in msg["reactions"]:
                emoji = rxn.get("emoji", "")
                if emoji not in emoji_counts:
                    emoji_counts[emoji] = {"count": 0, "user_ids": []}
                emoji_counts[emoji]["count"] += 1
                if rxn.get("reactor_id"):
                    emoji_counts[emoji]["user_ids"].append(rxn["reactor_id"])

            for emoji, data in emoji_counts.items():
                reactions.append(
                    ReactionResponse(
                        emoji=emoji,
                        count=data["count"],
                        user_ids=data["user_ids"],
                    )
                )

        items.append(
            MessageResponse(
                message_id=msg["message_id"],
                channel_id=msg["channel_id"],
                author_id=msg["author_id"],
                author_name=msg.get("author_name"),
                content=msg.get("content", ""),
                created_at=msg.get("created_at", 0),
                edited_at=msg.get("edited_at"),
                attachments=attachments,
                reactions=reactions,
            )
        )

    # Build cursors for pagination
    cursors = {"older": None, "newer": None}
    if items:
        cursors["older"] = items[-1].message_id if len(items) == limit else None
        cursors["newer"] = items[0].message_id

    return MessageListResponse(items=items, cursors=cursors)


@router.post(
    "/networks/{network_id}/channels/{channel_id}/messages",
    response_model=SendMessageResponse,
    status_code=201,
)
async def send_message(
    network_id: str,
    channel_id: str,
    request: SendMessageRequest,
):
    """Send a message to a channel."""
    peer_id = verify_network_access(network_id)
    db = get_db()
    t_ms = get_t_ms()

    # Convert from URL-safe base64
    channel_id = from_urlsafe_b64(channel_id)

    # Verify channel exists
    ch = channel_module.get_by_id(channel_id, recorded_by=peer_id, db=db)
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    try:
        result = message.create(
            peer_id=peer_id,
            channel_id=channel_id,
            content=request.text,
            t_ms=t_ms,
            db=db,
            return_latest=False,
        )
        db.commit()

        return SendMessageResponse(
            message_id=result["id"],
            attachments=[],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/networks/{network_id}/channels/{channel_id}/messages/with-attachments",
    response_model=SendMessageResponse,
    status_code=201,
)
async def send_message_with_attachments(
    network_id: str,
    channel_id: str,
    text: str = Form(""),
    attachments: list[UploadFile] = File(default=[]),
):
    """Send a message with file attachments.

    Accepts multipart/form-data with:
    - text: Message text (optional if attachments provided)
    - attachments: One or more files (images auto-compressed to 200KB)

    Image compression is automatic for image/* MIME types.
    """
    peer_id = verify_network_access(network_id)
    db = get_db()
    t_ms = get_t_ms()

    # Convert from URL-safe base64
    channel_id = from_urlsafe_b64(channel_id)

    # Verify channel exists
    ch = channel_module.get_by_id(channel_id, recorded_by=peer_id, db=db)
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Must have text or attachments
    if not text and not attachments:
        raise HTTPException(status_code=400, detail="Message must have text or attachments")

    try:
        # Create the message first
        result = message.create(
            peer_id=peer_id,
            channel_id=channel_id,
            content=text,
            t_ms=t_ms,
            db=db,
            return_latest=False,
        )
        message_id = result["id"]

        # Process attachments with chunked reading
        attachment_responses = []
        for upload in attachments:
            mime_type = upload.content_type

            # Read file in chunks to temp file (handles large uploads without OOM)
            with tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024) as tmp:
                total_size = 0
                while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                    tmp.write(chunk)
                    total_size += len(chunk)

                # Read back for processing
                tmp.seek(0)
                file_data = tmp.read()

            # Compress images automatically (200KB target, 2048px max dimension)
            file_data, compression_stats = message_attachment.compress_image_if_needed(
                file_data, mime_type
            )

            # Create attachment
            att_result = message_attachment.create(
                peer_id=peer_id,
                message_id=message_id,
                file_data=file_data,
                filename=upload.filename,
                mime_type=mime_type,
                t_ms=t_ms,
                db=db,
            )

            attachment_responses.append(
                AttachmentResponse(
                    file_id=att_result["file_id"],
                    filename=upload.filename,
                    mime_type=upload.content_type,
                    blob_bytes=att_result.get("blob_bytes"),
                )
            )

        db.commit()

        return SendMessageResponse(
            message_id=message_id,
            attachments=attachment_responses,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/networks/{network_id}/messages/{message_id}")
async def edit_message(network_id: str, message_id: str, request: EditMessageRequest):
    """Edit a message."""
    peer_id = verify_network_access(network_id)
    db = get_db()
    t_ms = get_t_ms()

    # Convert from URL-safe base64
    message_id = from_urlsafe_b64(message_id)

    try:
        message_update.create(
            peer_id=peer_id,
            message_id=message_id,
            new_content=request.text,
            t_ms=t_ms,
            db=db,
        )
        db.commit()
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/networks/{network_id}/messages/{message_id}")
async def delete_message(network_id: str, message_id: str):
    """Delete a message."""
    peer_id = verify_network_access(network_id)
    db = get_db()
    t_ms = get_t_ms()

    # Convert from URL-safe base64
    message_id = from_urlsafe_b64(message_id)

    try:
        message_deletion.create(
            peer_id=peer_id,
            message_id=message_id,
            t_ms=t_ms,
            db=db,
        )
        db.commit()
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
