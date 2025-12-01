"""Message attachment projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Message attachments contain file descriptor data (enc_key, root_hash, etc.)
and link to both a message and the file slices.

Validation: Only the message creator (signed_by) can attach files to their message.
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class MessageAttachmentEventData(TypedDict):
    type: str
    message_id: str
    file_id: str
    filename: NotRequired[str]
    mime_type: NotRequired[str]
    blob_bytes: int
    nonce_prefix: str  # base64
    enc_key: str  # base64
    root_hash: str  # base64
    total_slices: int
    signed_by: str
    created_at: int


class MessageAttachmentInput(TypedDict):
    event_id: str
    event_data: MessageAttachmentEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict  # message_signed_by: str (signed_by of the parent message)


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": True,  # Group-wrapped
    "signer_type": "peer_shared",  # Standard peer signature
    "dependencies": ["message:message_id"],  # Depends on the parent message
    "tables": ["message_attachments", "event_dependencies"],
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: MessageAttachmentInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Validates message_attachment and outputs table rows.
    Enforces that only the message creator can attach files.
    """
    import crypto

    event_id = input_dict["event_id"]
    event_data: MessageAttachmentEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict.get("dependencies", {})

    message_id = event_data.get("message_id")
    file_id = event_data.get("file_id")
    filename = event_data.get("filename")
    mime_type = event_data.get("mime_type")
    signed_by = event_data.get("signed_by")

    # File descriptor fields
    blob_bytes = event_data.get("blob_bytes")
    nonce_prefix_b64 = event_data.get("nonce_prefix")
    enc_key_b64 = event_data.get("enc_key")
    root_hash_b64 = event_data.get("root_hash")
    total_slices = event_data.get("total_slices")

    # Validate required fields
    if not all([message_id, file_id, signed_by, blob_bytes is not None,
                nonce_prefix_b64, enc_key_b64, root_hash_b64, total_slices is not None]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Validate: attachment creator must match message creator
    message_signed_by = deps.get("message_signed_by")
    if not message_signed_by:
        # Message not found or not synced yet - block
        return ProjectorResult(
            valid=True,
            blocked=True,
            reason="waiting for parent message"
        )

    if message_signed_by != signed_by:
        return ProjectorResult(
            valid=False,
            reason=f"attachment signed_by does not match message signed_by"
        )

    # Decode file descriptor fields
    nonce_prefix = crypto.b64decode(nonce_prefix_b64)
    enc_key = crypto.b64decode(enc_key_b64)
    root_hash = crypto.b64decode(root_hash_b64)

    # Output: message_attachments row
    attachment_row = {
        "message_id": message_id,
        "file_id": file_id,
        "filename": filename,
        "mime_type": mime_type,
        "blob_bytes": blob_bytes,
        "nonce_prefix": nonce_prefix,
        "enc_key": enc_key,
        "root_hash": root_hash,
        "total_slices": total_slices,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    # Output: event_dependencies row (attachment depends on message)
    dep_row = {
        "child_event_id": event_id,
        "parent_event_id": message_id,
        "recorded_by": recorded_by,
        "dependency_type": "message",
    }

    return ProjectorResult(
        valid=True,
        tables={
            "message_attachments": [attachment_row],
            "event_dependencies": [dep_row],
        },
    )


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    message_id: str = "msg_123",
    file_id: str = "file_123",
    filename: str | None = "test.txt",
    mime_type: str | None = "text/plain",
    blob_bytes: int = 1024,
    nonce_prefix: str = "AAAAAAAAAAAAAAAAAAAAAA==",  # 16 bytes base64
    enc_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 bytes base64
    root_hash: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 bytes base64
    total_slices: int = 3,
    signed_by: str = "peer_shared_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "message_attachment",
        "message_id": message_id,
        "file_id": file_id,
        "filename": filename,
        "mime_type": mime_type,
        "blob_bytes": blob_bytes,
        "nonce_prefix": nonce_prefix,
        "enc_key": enc_key,
        "root_hash": root_hash,
        "total_slices": total_slices,
        "signed_by": signed_by,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "attachment_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    message_signed_by: str | None = "peer_shared_123",  # Must match event_data.signed_by
) -> dict:
    """Build complete input dict for testing.

    Note: For validation to pass, message_signed_by must match
    event_data.signed_by (the message creator must be the attachment creator).
    """
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {
            "message_signed_by": message_signed_by,
        },
    }
