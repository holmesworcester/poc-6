"""Message reaction deletion event type - handles deletion of emoji reactions.

This is the deletion counterpart to message_reaction.
When a reaction is removed, a message_reaction_deletion event is created.
"""
from typing import Any
import logging

log = logging.getLogger(__name__)

# Event type declarations for auto-discovery
EVENT_TYPE = 'message_reaction_deletion'
SHAREABLE = True  # Sync deletions to other peers
EPHEMERAL = False
PROJECTION_TABLE = ('message_reaction_deletions', 'deletion_id')


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project message_reaction_deletion event.

    Delegates to message_reaction.project_deletion() to handle the deletion logic.

    Args:
        event_id: Deletion event ID
        recorded_by: Peer who recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection

    Returns:
        event_id if successful, None if blocked
    """
    # Import here to avoid circular dependency
    from events.content import message_reaction

    # Delegate to message_reaction.project_deletion()
    message_reaction.project_deletion(event_id, recorded_by, recorded_at, db)

    # Return event_id to mark as valid
    return event_id
