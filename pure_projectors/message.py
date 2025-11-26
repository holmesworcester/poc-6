"""Pure functional projector for message events.

Input dict structure:
{
    "event_id": str,
    "event_data": {  # decrypted, verified event payload
        "type": "message",
        "channel_id": str,
        "group_id": str,
        "signed_by": str,
        "author_id": str,
        "content": str,
        "created_at": int
    },
    "key_id": str,  # extracted from blob for purge lookups
    "recorded_by": str,
    "recorded_at": int,
    "dependencies": {
        "channel": {  # the channel this message belongs to
            "event_id": str,
            "event_data": {
                "name": str,
                "group_id": str,
                "disappearing_time_ms": int,
                ...
            }
        },
        "deletion": {  # optional - if a deletion event exists for this message
            "event_id": str,
            "deleted_by": str,
            "is_valid": bool  # pre-computed by resolver
        } | None
    }
}

Output:
{
    "messages": [row],
    "event_dependencies": [row]  # for cascading deletion tracking
}
"""

from pure_projectors.framework import projector, ProjectorResult


# Default message TTL: 1 week (in milliseconds)
DEFAULT_MESSAGE_TTL_MS = 7 * 24 * 60 * 60 * 1000


@projector(
    event_type="message",
    tables=["messages", "event_dependencies", "deleted_events"]
)
def project(input_dict: dict) -> ProjectorResult:
    """Pure functional projector for message events.

    Takes a dict with the event and its resolved dependencies.
    Returns a dict of table rows to insert.
    """
    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    key_id = input_dict["key_id"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict["dependencies"]

    # Check if deletion exists and is valid
    deletion = deps.get("deletion")
    if deletion and deletion.get("is_valid"):
        # Message has valid deletion - don't project message, but record deletion
        return ProjectorResult(
            valid=True,
            tables={
                "deleted_events": [{
                    "event_id": event_id,
                    "recorded_by": recorded_by
                }]
            }
        )

    # Get channel dependency
    channel = deps.get("channel")
    if not channel:
        return ProjectorResult(
            blocked=True,
            missing_deps=["channel"]
        )

    channel_data = channel["event_data"]

    # Calculate TTL from channel's disappearing_time_ms
    created_at = event_data["created_at"]
    disappearing_time_ms = channel_data.get("disappearing_time_ms", 0)

    if disappearing_time_ms and disappearing_time_ms > 0:
        ttl_ms = created_at + disappearing_time_ms
    else:
        ttl_ms = 0  # Permanent

    # Build message row
    message_row = {
        "message_id": event_id,
        "channel_id": event_data["channel_id"],
        "group_id": event_data["group_id"],
        "author_id": event_data["author_id"],
        "signed_by": event_data["signed_by"],
        "content": event_data.get("content", ""),
        "created_at": created_at,
        "ttl_ms": ttl_ms,
        "key_id": key_id,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at
    }

    # Build dependency tracking row
    dep_row = {
        "child_event_id": event_id,
        "parent_event_id": event_data["channel_id"],
        "recorded_by": recorded_by,
        "dependency_type": "channel"
    }

    return ProjectorResult(
        valid=True,
        tables={
            "messages": [message_row],
            "event_dependencies": [dep_row]
        }
    )
