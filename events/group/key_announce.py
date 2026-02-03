"""Key announcement event type (announces that a key exists for a group).

When a new secret is created for a group, a key_announce event is also created
to let all group members know the key exists. Members who don't have the key
can then create key_request events to obtain it.

This solves the "how do I know I'm missing a key?" problem for:
- Late joiners who need historical keys
- Partition healing where one side created new keys
"""

# Registry metadata
EVENT_TYPE = 'key_announce'
SHAREABLE = True  # Syncs to all peers
PROJECTION_TABLE = ('key_announces', 'announce_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Event specification - signed, shareable
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for key_announce events.

    Records the announcement so we can scan for missing keys later.
    """
    event_data = ctx.event_data

    key_id = event_data.get('key_id')
    group_id = event_data.get('group_id')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')
    removal_epoch_id = event_data.get('removal_epoch_id')  # Can be None for initial keys

    if not key_id or not group_id or not signed_by or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='key_announces',
            values={
                'announce_id': ctx.event_id,
                'key_id': key_id,
                'group_id': group_id,
                'removal_epoch_id': removal_epoch_id,
                'signed_by': signed_by,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(key_id: str, group_id: str, peer_id: str, peer_shared_id: str,
           removal_epoch_id: str | None, t_ms: int, db: Any) -> str:
    """Create a key announcement for a group.

    Args:
        key_id: The key being announced
        group_id: Which group this key is for
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (signer identity)
        removal_epoch_id: Current removal epoch (keys require this epoch to fulfill requests)
        t_ms: Timestamp
        db: Database connection

    Returns:
        announce_id: The event ID
    """
    log.info(f"key_announce.create() key={key_id[:20]}..., group={group_id[:20]}..., epoch={removal_epoch_id}")

    # Get private key for signing
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wire event
    blob = wire_format.encode_key_announce_wire_event(
        key_id_b64=key_id,
        group_id_b64=group_id,
        removal_epoch_id_b64=removal_epoch_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )

    # Store event
    announce_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"key_announce.create() created announce_id={announce_id}")

    return announce_id


def get_announced_keys_for_group(group_id: str, peer_id: str, db: Any) -> list[dict[str, Any]]:
    """Get all announced keys for a specific group.

    Args:
        group_id: The group to check
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of key announcements
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT * FROM key_announces
           WHERE group_id = ? AND recorded_by = ?
           ORDER BY created_at ASC""",
        (group_id, peer_id)
    ))


def get_missing_keys_for_group(group_id: str, peer_id: str, db: Any) -> list[str]:
    """Get keys that have been announced but we don't have.

    Args:
        group_id: The group to check
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of key_ids we're missing
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get announced keys that we don't have in secrets table
    rows = safedb.query(
        """SELECT ka.key_id FROM key_announces ka
           WHERE ka.group_id = ? AND ka.recorded_by = ?
           AND NOT EXISTS (
               SELECT 1 FROM secrets s
               WHERE s.secret_id = ka.key_id AND s.recorded_by = ?
           )""",
        (group_id, peer_id, peer_id)
    )

    return [row['key_id'] for row in rows]


def get_all_missing_keys(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """Get all announced keys we're missing across all groups.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of {key_id, group_id} dicts for missing keys
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get announced keys that we don't have in secrets table
    rows = safedb.query(
        """SELECT DISTINCT ka.key_id, ka.group_id FROM key_announces ka
           WHERE ka.recorded_by = ?
           AND NOT EXISTS (
               SELECT 1 FROM secrets s
               WHERE s.secret_id = ka.key_id AND s.recorded_by = ?
           )""",
        (peer_id, peer_id)
    )

    return [{'key_id': row['key_id'], 'group_id': row['group_id']} for row in rows]


def list_announces(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all key announcements.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of announcement records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT * FROM key_announces
           WHERE recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id,)
    ))


def get_removal_epoch_for_key(key_id: str, peer_id: str, db: Any) -> str | None:
    """Get the removal epoch associated with a key.

    This is critical for key request fulfillment: a key can only be shared
    with peers who are not removed in the key's removal epoch.

    Args:
        key_id: The key to look up
        peer_id: Local peer ID
        db: Database connection

    Returns:
        removal_epoch_id or None if key not found or no epoch
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    row = safedb.query_one(
        """SELECT removal_epoch_id FROM key_announces
           WHERE key_id = ? AND recorded_by = ?
           LIMIT 1""",
        (key_id, peer_id)
    )
    return row['removal_epoch_id'] if row else None
