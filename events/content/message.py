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
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db
from events.identity import peer
from events.group import group

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
    "generic_dispatch": True,
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def create(deps: CreateDeps, content: str, t_ms: int):
    """Pure function to create a message event.

    Args:
        deps: Resolved dependencies
        content: Message content
        t_ms: Timestamp in milliseconds

    Returns:
        CreateResult with blob and computed event_id
    """
    from projection import CreateResult, BlobSpec, compute_event_id

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


def project(input_dict: dict):
    """Pure projection: dict -> result. No database access."""
    from projection import ProjectorResult

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
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

    Args:
        peer_id: Local peer ID creating the message
        channel_id: Channel to post message in
        content: Message content
        t_ms: Timestamp
        db: Database connection
        return_latest: If True (default), return list of recent messages.

    Returns:
        {'id': message_id, 'latest': list of recent messages}
    """
    from projection import store_create_result

    log.info(f"message.send() sending to channel_id={channel_id}, content='{content[:50]}...'")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Resolve dependencies inline
    # 1. Get channel info
    channel_row = safedb.query_one(
        "SELECT group_id, disappearing_time_ms FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (channel_id, peer_id)
    )
    if not channel_row:
        raise ValueError(f"Channel not found: {channel_id}")

    # 2. Get peer_self info
    peer_row = safedb.query_one(
        "SELECT peer_shared_id, user_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (peer_id, peer_id)
    )
    if not peer_row:
        raise ValueError(f"Peer self not found: {peer_id}")
    if not peer_row.get('user_id'):
        raise ValueError(f"User identity not set for peer {peer_id}. Must create or link account first.")

    # 3. Get private key and group key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    key_data = group.pick_key(channel_row['group_id'], peer_id, db)

    deps = {
        'channel_id': channel_id,
        'group_id': channel_row['group_id'],
        'disappearing_time_ms': channel_row['disappearing_time_ms'] or 0,
        'peer_shared_id': peer_row['peer_shared_id'],
        'user_id': peer_row['user_id'],
        'private_key': private_key,
        'key_data': key_data,
    }

    # Pure create
    result = create(deps, content, t_ms)

    # Store and project
    event_id = store_create_result(result, peer_id, t_ms, db)

    log.info(f"message.send() created message_id={event_id}")

    latest = list(channel_id, peer_id, db) if return_latest else []

    return {
        'id': event_id,
        'latest': latest
    }


def list(channel_id: int, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List messages in a channel."""
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


# project_event() handled by generic dispatch (SPEC.generic_dispatch = True)
