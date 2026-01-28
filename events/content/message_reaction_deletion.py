"""Message reaction deletion event type - handles deletion of emoji reactions.

This is the deletion counterpart to message_reaction.
When a reaction is removed, a message_reaction_deletion event is created.
"""
from typing import Any
import logging
from core import crypto
from core import wire_format
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
    'requires': {
        'reaction': {
            'source': 'table',
            'table': 'message_reactions',
            'key': 'reaction_id',
            'fields': ['reaction_id', 'reactor_id'],
        },
    },
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

    _wire_shadow_message_reaction_deletion(reaction_id)

    reaction_row = ctx.deps.get('reaction') or {}
    signer = ctx.signer or {}
    signer_user_id = signer.get('user_id')
    reactor_id = reaction_row.get('reactor_id')
    if not signer_user_id or not reactor_id or signer_user_id != reactor_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='delete',
            table='message_reactions',
            values={},
            where={
                'reaction_id': reaction_id,
                'recorded_by': ctx.recorded_by,
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


def _wire_shadow_message_reaction_deletion(reaction_id: str) -> None:
    """Validate message_reaction_deletion fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_message_reaction_deletion_plaintext(
        reaction_id=crypto.b64decode(reaction_id)
    )
    decoded = wire_format.decode_message_reaction_deletion_plaintext(plaintext)
    if decoded["reaction_id"] != crypto.b64decode(reaction_id):
        raise ValueError("wire shadow decode reaction_id mismatch")
