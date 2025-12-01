"""Message event type.

Pure functions:
    create(deps, content, t_ms) -> CreateResult
    project(input_dict) -> ProjectorResult

API functions:
    send(peer_id, channel_id, content, t_ms, db) -> dict
    list(channel_id, recorded_by, db) -> list
    project_event(event_id, recorded_by, recorded_at, db) -> str | None
"""
from typing import Any, TypedDict, NotRequired
from dataclasses import dataclass, field
import logging
import crypto
from db import create_safe_db

log = logging.getLogger(__name__)

DEFAULT_TTL_MS = 7 * 24 * 60 * 60 * 1000  # 1 week


# ============================================================================
# TYPES
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


class CreateDeps(TypedDict):
    """Dependencies resolved for message creation."""
    channel_id: str
    group_id: str
    disappearing_time_ms: int
    peer_shared_id: str
    user_id: str
    private_key: bytes
    key_data: dict  # {id, key, type} for crypto.wrap


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
    "channel": {
        "table": "channels",
        "key_field": "channel_id",
        "fields": ["group_id", "disappearing_time_ms"],
    },
    "peer_self": {
        "table": "peer_self",
        "key_field": "peer_id",
        "fields": ["peer_shared_id", "user_id"],
    },
    "private_key": {"type": "local_peer_key"},
    "key_data": {"type": "group_key", "from": "channel.group_id"},
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def create_event(deps: CreateDeps, content: str, t_ms: int):
    """Pure function to create a message event.

    Args:
        deps: Resolved dependencies (from resolve_create_deps)
        content: Message content
        t_ms: Timestamp in milliseconds

    Returns:
        CreateResult with blob and computed event_id
    """
    from projectors import CreateResult, BlobSpec, compute_event_id

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

    signed_event = crypto.sign_event(event_data, deps['private_key'])
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, deps['key_data'], None)
    event_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=event_id, event_type='message')],
        primary_id=event_id,
    )


def project(input_dict: MessageInput):
    """Pure projection: dict -> result. No database access."""
    from projectors import ProjectorResult

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
# TEST BUILDERS
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


# ============================================================================
# API FUNCTIONS
# ============================================================================

def send(peer_id: str, channel_id: str, content: str, t_ms: int, db: Any, return_latest: bool = True) -> dict[str, Any]:
    """Send a message to a channel.

    Uses pure functional create pattern:
    1. resolve_create_deps() gathers dependencies from DB
    2. create() builds blob (no DB access)
    3. store_create_result() stores and projects

    Args:
        peer_id: Local peer ID creating the message
        channel_id: Channel to post message in
        content: Message content
        t_ms: Timestamp
        db: Database connection
        return_latest: If True (default), return list of recent messages.

    Returns:
        {'id': message_id, 'latest': list of recent messages (or empty list if return_latest=False)}
    """
    from projectors import resolve_create_deps, store_create_result

    log.info(f"message.send() sending to channel_id={channel_id}, content='{content[:50]}...'")

    # 1. Resolve dependencies (DB queries)
    deps = resolve_create_deps("message", {"channel_id": channel_id}, peer_id, db)

    if not deps.get('user_id'):
        raise ValueError(f"User identity not set for peer {peer_id}. Must create or link account first.")

    # 2. Pure create (no DB access)
    result = create_event(deps, content, t_ms)

    # 3. Store and project
    event_id = store_create_result(result, peer_id, t_ms, db)

    log.info(f"message.send() created message_id={event_id}")

    latest = list(channel_id, peer_id, db) if return_latest else []

    return {
        'id': event_id,
        'latest': latest
    }


def list(channel_id: int, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List messages in a channel for a specific peer, including attachments and author names.

    Returns message dicts with 'attachments' field containing list of attached files
    and 'author_name' field from the users table:
    [{'message_id', 'content', 'author_id', 'author_name', 'created_at', 'attachments': [...]}, ...]
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    messages = safedb.query(
        """SELECT m.*, u.name as author_name
           FROM messages m
           LEFT JOIN users u ON m.author_id = u.user_id AND m.recorded_by = u.recorded_by
           WHERE m.channel_id = ? AND m.recorded_by = ?
           ORDER BY m.created_at DESC LIMIT 50""",
        (channel_id, recorded_by)
    )

    for msg in messages:
        attachments = safedb.query(
            """SELECT ma.file_id, ma.filename, ma.mime_type, ma.blob_bytes
               FROM message_attachments ma
               WHERE ma.message_id = ? AND ma.recorded_by = ?
               ORDER BY ma.recorded_at ASC""",
            (msg['message_id'], recorded_by)
        )
        msg['attachments'] = attachments if attachments else []

    return messages


# Backward compatibility alias
create = send


def project_event(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project a single message event into the database.

    This is the impure wrapper that:
    1. Resolves dependencies from DB
    2. Calls pure project()
    3. Applies result to DB
    """
    log.debug(f"message.project_event() projecting message_id={event_id}, seen_by={recorded_by}")

    from projectors import resolve, apply_result

    safedb = create_safe_db(db, recorded_by=recorded_by)

    input_dict = resolve("message", event_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    # Side effect: cleanup invalid deletion
    deletion = input_dict["dependencies"].get("deletion")
    if deletion and not deletion.get("is_valid"):
        safedb.execute(
            "DELETE FROM message_deletions WHERE message_id = ? AND recorded_by = ?",
            (event_id, recorded_by)
        )

    result = project(input_dict)

    if result.blocked or not result.valid:
        return None

    apply_result(result, recorded_by, recorded_at, db)

    if "messages" in result.tables:
        log.info(f"message.project_event() projected id={event_id[:20]}...")
        return event_id
    return None
