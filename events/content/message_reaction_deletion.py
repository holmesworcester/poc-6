"""Message reaction deletion event type - handles deletion of emoji reactions.

This is the deletion counterpart to message_reaction.
When a reaction is removed, a message_reaction_deletion event is created.
"""
from typing import Any
import logging
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)

# Event type declarations for auto-discovery
EVENT_TYPE = 'message_reaction_deletion'
SHAREABLE = True  # Sync deletions to other peers
PROJECTION_TABLE = ('message_reaction_deletions', 'deletion_id')

# v2 event specification - signed by peer_shared, encrypted
EVENT_SPEC = {
    'encrypted': True,
    'signer': {
        'id_field': 'deleted_by',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for message_reaction_deletion events."""
    event_data = ctx.event_data

    reaction_id = event_data.get('reaction_id')
    deleted_by = event_data.get('deleted_by')
    created_at = event_data.get('created_at')

    if not all([reaction_id, deleted_by, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='delete',
            table='message_reactions',
            values={},
            where={
                'reaction_id': reaction_id,
            },
        ),
        WriteOp(
            op='insert',
            table='message_reaction_deletions',
            values={
                'deletion_id': ctx.event_id,
                'reaction_id': reaction_id,
                'deleted_by': deleted_by,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


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
