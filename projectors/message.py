"""Message projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

DEPS - declares dependencies needed for creation
create_pure() - pure function: deps -> CreateResult (no DB access)
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult, CreateResult, BlobSpec, compute_event_id
import logging
import crypto

log = logging.getLogger(__name__)

DEFAULT_TTL_MS = 7 * 24 * 60 * 60 * 1000  # 1 week


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class MessageEventData(TypedDict):
    type: str
    channel_id: str
    group_id: str
    content: str
    author_id: str
    signed_by: str
    created_at: int
    disappearing_time_ms: NotRequired[int]


class DeletionDep(TypedDict):
    deleted_by: str
    is_valid: bool


class MessageInput(TypedDict):
    event_id: str
    event_data: MessageEventData
    key_id: str
    recorded_by: str
    recorded_at: int
    dependencies: dict  # {"deletion": DeletionDep | None}


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": True,
    "signer_type": "peer_shared",
    "dependencies": ["deletion:message_deletion?"],
    "tables": ["messages", "event_dependencies", "deleted_events"],
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    # From channels table: need group_id and disappearing_time_ms
    "channel": {
        "table": "channels",
        "key_field": "channel_id",
        "fields": ["group_id", "disappearing_time_ms"],
    },
    # From peer_self table: need peer_shared_id and user_id
    "peer_self": {
        "table": "peer_self",
        "key_field": "peer_id",
        "fields": ["peer_shared_id", "user_id"],
    },
    # Private key for signing
    "private_key": {"type": "local_peer_key"},
    # Group key for encryption (looked up via group_id from channel)
    "key_data": {"type": "group_key", "from": "channel.group_id"},
}


# ============================================================================
# CREATE - pure function: deps -> CreateResult
# ============================================================================

class CreateDeps(TypedDict):
    """Dependencies resolved for message creation."""
    channel_id: str
    group_id: str
    disappearing_time_ms: int
    peer_shared_id: str
    user_id: str
    private_key: bytes
    key_data: dict  # {id, key, type} for crypto.wrap


def create_pure(deps: CreateDeps, content: str, t_ms: int) -> CreateResult:
    """Pure function to create a message event.

    Args:
        deps: Resolved dependencies (from resolve_create_deps)
        content: Message content
        t_ms: Timestamp in milliseconds

    Returns:
        CreateResult with blob and computed event_id
    """
    event_data = {
        'type': 'message',
        'channel_id': deps['channel_id'],
        'group_id': deps['group_id'],
        'signed_by': deps['peer_shared_id'],
        'author_id': deps['user_id'],
        'content': content,
        'created_at': t_ms,
        'disappearing_time_ms': deps['disappearing_time_ms'],
    }

    # Sign the event
    signed_event = crypto.sign_event(event_data, deps['private_key'])

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, deps['key_data'], None)

    # Compute event ID (content-addressed)
    event_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=event_id, event_type='message')],
        primary_id=event_id,
    )


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: MessageInput) -> ProjectorResult:
    """Pure projection: dict -> result. No database access."""
    event_id = input_dict["event_id"]
    event_data: MessageEventData = input_dict["event_data"]
    key_id = input_dict["key_id"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict["dependencies"]

    # Valid deletion -> skip message projection
    deletion = deps.get("deletion")
    if deletion and deletion.get("is_valid"):
        return ProjectorResult(
            valid=True,
            tables={"deleted_events": [{"event_id": event_id, "recorded_by": recorded_by}]}
        )

    # Calculate TTL
    created_at = event_data.get("created_at", 0)
    disappearing_time_ms = event_data.get("disappearing_time_ms", 0) or 0

    if disappearing_time_ms > 0:
        ttl_ms = created_at + disappearing_time_ms
    elif "disappearing_time_ms" in event_data:
        ttl_ms = 0  # Explicit permanent
    else:
        ttl_ms = created_at + DEFAULT_TTL_MS

    message_row = {
        "message_id": event_id,
        "channel_id": event_data.get("channel_id"),
        "group_id": event_data.get("group_id"),
        "content": event_data.get("content"),
        "author_id": event_data.get("author_id"),
        "signed_by": event_data.get("signed_by"),
        "created_at": created_at,
        "key_id": key_id,
        "ttl_ms": ttl_ms,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    dep_row = {
        "child_event_id": event_id,
        "parent_event_id": event_data.get("channel_id"),
        "recorded_by": recorded_by,
        "dependency_type": "channel",
    }

    return ProjectorResult(
        valid=True,
        tables={"messages": [message_row], "event_dependencies": [dep_row]},
    )


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    content: str = "test message",
    channel_id: str = "ch_123",
    group_id: str = "grp_123",
    author_id: str = "user_123",
    signed_by: str = "peer_123",
    created_at: int = 1000000,
    disappearing_time_ms: int | None = None,
) -> dict:
    """Build event_data for testing."""
    data = {
        "type": "message",
        "content": content,
        "channel_id": channel_id,
        "group_id": group_id,
        "author_id": author_id,
        "signed_by": signed_by,
        "created_at": created_at,
    }
    if disappearing_time_ms is not None:
        data["disappearing_time_ms"] = disappearing_time_ms
    return data


def make_input(
    event_id: str = "msg_123",
    event_data: dict | None = None,
    key_id: str = "key_123",
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    deletion: dict | None = None,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "key_id": key_id,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {"deletion": deletion},
    }
