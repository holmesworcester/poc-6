"""Message deletion projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

The projector only handles authorization. The framework handles:
- Recording deleted events (apply_result)
- Cascade deletion from valid_events (cleanup_deleted_events)
- Cleaning projected data (cleanup_deleted_events)
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult, CreateResult, BlobSpec, compute_event_id
import logging
import crypto

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class MessageDeletionEventData(TypedDict):
    type: str
    message_id: str
    signed_by: str  # peer_shared_id of deleter
    created_at: int


class MessageDep(TypedDict):
    message_id: str
    author_id: str
    group_id: str
    channel_id: str


class MessageDeletionInput(TypedDict):
    event_id: str
    event_data: MessageDeletionEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict  # {"message_event": MessageDep | None}


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": True,
    "signer_type": "peer_shared",
    "dependencies": ["message:message_event"],  # Blocking dep for authorization (name:type)
    "tables": [],  # No type-specific table - uses generic deleted_events
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    # Private key for signing
    "private_key": {"type": "local_peer_key"},
    # peer_shared_id from peer_self
    "peer_self": {"table": "peer_self", "key_field": "peer_id", "fields": ["peer_shared_id"]},
    # message info for group_id (to get encryption key)
    "message": {"table": "messages", "key_field": "message_id", "fields": ["group_id"]},
    # Group key for encryption
    "key_data": {"type": "group_key", "from": "message.group_id"},
}


# ============================================================================
# CREATE - pure function: deps -> CreateResult
# ============================================================================

class MessageDeletionCreateDeps(TypedDict):
    """Dependencies resolved for message_deletion creation."""
    peer_shared_id: str
    private_key: bytes
    key_data: dict  # {id, key, type} for crypto.wrap
    message_id: str
    group_id: str


def create_pure(
    deps: MessageDeletionCreateDeps,
    t_ms: int,
) -> CreateResult:
    """Pure function to create a message_deletion event.

    Message deletions are encrypted to the same group as the message
    to ensure only group members can see the deletion.

    Args:
        deps: Resolved dependencies
        t_ms: Timestamp

    Returns:
        CreateResult with deletion blob
    """
    event_data = {
        'type': 'message_deletion',
        'message_id': deps['message_id'],
        'signed_by': deps['peer_shared_id'],
        'created_at': t_ms,
    }

    # Sign the event
    signed_event = crypto.sign_event(event_data, deps['private_key'])

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, deps['key_data'], None)
    deletion_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=deletion_id, event_type='message_deletion')],
        primary_id=deletion_id,
    )


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: MessageDeletionInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Authorization check: deleter must be author OR admin.
    Output: deleted_events=[message_id] - framework handles cascade.
    """
    event_data: MessageDeletionEventData = input_dict["event_data"]
    deps = input_dict.get("dependencies", {})

    message_id = event_data.get("message_id")
    deleted_by = event_data.get("signed_by")

    if not message_id or not deleted_by:
        return ProjectorResult(valid=False, reason="missing message_id or signed_by")

    # Get message for authorization (dependency name is "message", type is "message_event")
    message = deps.get("message")
    if not message:
        # Message not yet valid - block and retry later
        return ProjectorResult(blocked=True, missing_deps=["message"])

    # Authorization: deleted_by must be author OR admin
    message_author = message.get("author_id")
    is_author = (deleted_by == message_author)

    # Admin check - resolved by framework via is_admin dependency
    # For now, check is_admin in deps (will be added to resolve())
    is_admin = deps.get("is_admin", False)

    if not is_author and not is_admin:
        log.warning(f"message_deletion: not authorized. deleted_by={deleted_by[:20]}..., author={message_author[:20] if message_author else 'None'}...")
        return ProjectorResult(valid=False, reason="not authorized to delete")

    # Output: mark message as deleted
    # Framework handles: recording to deleted_events, cascade, data cleanup
    return ProjectorResult(
        valid=True,
        deleted_events=[message_id],
    )


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    message_id: str = "msg_123",
    signed_by: str = "peer_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "message_deletion",
        "message_id": message_id,
        "signed_by": signed_by,
        "created_at": created_at,
    }


def make_message_dep(
    message_id: str = "msg_123",
    author_id: str = "peer_123",
    group_id: str = "grp_123",
    channel_id: str = "ch_123",
) -> dict:
    """Build message dependency for testing."""
    return {
        "message_id": message_id,
        "author_id": author_id,
        "group_id": group_id,
        "channel_id": channel_id,
    }


def make_input(
    event_id: str = "del_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    message_event: dict | None = None,
    is_admin: bool = False,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {
            "message_event": message_event,
            "is_admin": is_admin,
        },
    }
