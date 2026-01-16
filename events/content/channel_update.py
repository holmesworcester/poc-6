"""Channel update event type (shareable, encrypted).

Allows admins to update channel name or disappearing_time_ms after creation.
Uses global_count for convergent update ordering (highest wins).
"""

# Registry metadata
EVENT_TYPE = 'channel_update'
SHAREABLE = True  # Channel updates sync to all members
EPHEMERAL = False
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from events.content import channel
from events.identity import invite as invite_module
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)

EVENT_SPEC = {
    'encrypted': True,
    'signer': {
        'id_field': 'updated_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'channel': {
            'source': 'table',
            'table': 'channels',
            'key': 'channel_id',
            'fields': ['channel_id', 'group_id'],
        },
    },
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for channel_update events."""
    event_data = ctx.event_data

    channel_id = event_data.get('channel_id')
    group_id = event_data.get('group_id')
    updated_by = event_data.get('updated_by')
    global_count = event_data.get('global_count')
    new_channel_name = event_data.get('new_channel_name')
    new_disappearing_time_ms = event_data.get('new_disappearing_time_ms')
    created_at = event_data.get('created_at')

    if not all([channel_id, group_id, updated_by, global_count is not None, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    if new_channel_name is None and new_disappearing_time_ms is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    if new_channel_name is not None and not new_channel_name.strip():
        return ProjectorResult(writes=tuple(), valid_event=False)

    if new_disappearing_time_ms is not None and new_disappearing_time_ms < 0:
        return ProjectorResult(writes=tuple(), valid_event=False)

    channel_row = ctx.deps.get('channel')
    if not channel_row or channel_row.get('group_id') != group_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    signer = ctx.signer or {}
    if signer.get('type') != 'peer_shared':
        return ProjectorResult(writes=tuple(), valid_event=False)
    if not signer.get('is_admin'):
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='channel_updates',
            values={
                'update_id': ctx.event_id,
                'channel_id': channel_id,
                'updated_by': updated_by,
                'global_count': global_count,
                'new_channel_name': new_channel_name,
                'new_disappearing_time_ms': new_disappearing_time_ms,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _validate_admin(peer_shared_id: str, recorded_by: str, db: Any) -> bool:
    """Validate that peer_shared_id is an admin.

    Args:
        peer_shared_id: Public peer ID to check
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if user is admin, False otherwise
    """
    return invite_module.is_admin(peer_shared_id, recorded_by, db)


def create(
    channel_id: str,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
    new_channel_name: str | None = None,
    new_disappearing_time_ms: int | None = None,
) -> str:
    """Create a channel update event (encrypted, shareable).

    Only admins can update channels. At least one of new_channel_name or
    new_disappearing_time_ms must be provided.

    Args:
        channel_id: Channel to update
        peer_id: Local peer ID (for signing and seeing)
        peer_shared_id: Public peer ID (for updated_by)
        t_ms: Timestamp
        db: Database connection
        new_channel_name: New channel name (or None to keep existing)
        new_disappearing_time_ms: New disappearing time in ms (or None to keep existing)

    Returns:
        update_id: The created update event ID

    Raises:
        ValueError: If not authorized, channel not found, or invalid parameters
    """
    # Check authorization - only admins can update channels
    if not _validate_admin(peer_shared_id, peer_id, db):
        raise ValueError(f"User {peer_shared_id} not authorized to update channels (only admins can)")

    # Validate at least one field is provided
    if new_channel_name is None and new_disappearing_time_ms is None:
        raise ValueError("At least one field (name or disappearing_time_ms) must be provided")

    # Get channel to find group_id
    channel_row = channel.get(channel_id, peer_id, db)
    if not channel_row:
        raise ValueError(f"Channel {channel_id} not found")

    group_id = channel_row['group_id']

    # Validate new values
    if new_channel_name is not None and not new_channel_name.strip():
        raise ValueError("Channel name cannot be empty")

    if new_disappearing_time_ms is not None and new_disappearing_time_ms < 0:
        raise ValueError("disappearing_time_ms must be non-negative")

    # Calculate global_count (max existing + 1)
    global_count = channel.get_next_update_count(channel_id, peer_id, db)

    # Build update event
    event_data = {
        'type': 'channel_update',
        'channel_id': channel_id,
        'group_id': group_id,
        'updated_by': peer_shared_id,
        'signer_type': 'peer_shared',
        'created_at': t_ms,
        'global_count': global_count,
        'new_channel_name': new_channel_name,
        'new_disappearing_time_ms': new_disappearing_time_ms,
    }

    event_id = store.publish(event_data, group_id, peer_id, t_ms, db)

    log.info(
        f"channel_update.create() created update_id={event_id} for channel_id={channel_id}, "
        f"name={new_channel_name}, ttl={new_disappearing_time_ms}"
    )

    return event_id


def validate(update_id: str, recorded_by: str, db: Any) -> bool:
    """Validate a channel update event.

    Checks:
    1. Creator is an admin
    2. Channel exists
    3. Values are valid (non-negative disappearing_time, non-empty name if provided)

    Args:
        update_id: Event ID of the update
        recorded_by: Peer perspective for validation
        db: Database connection

    Returns:
        True if valid, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get the update from the table
    update_row = safedb.query_one(
        "SELECT * FROM channel_updates WHERE update_id = ? AND recorded_by = ? LIMIT 1",
        (update_id, recorded_by)
    )

    if not update_row:
        log.warning(f"channel_update.validate() update not found for update_id={update_id}")
        return False

    # Check admin authorization
    if not _validate_admin(update_row['updated_by'], recorded_by, db):
        log.warning(f"channel_update.validate() updater {update_row['updated_by']} is not admin")
        return False

    # Check channel exists
    channel_row = safedb.query_one(
        "SELECT channel_id FROM channels WHERE channel_id = ? AND recorded_by = ? LIMIT 1",
        (update_row['channel_id'], recorded_by)
    )

    if not channel_row:
        log.warning(f"channel_update.validate() channel not found for channel_id={update_row['channel_id']}")
        return False

    # Validate field values
    if (update_row['new_channel_name'] is not None and
        not update_row['new_channel_name'].strip()):
        log.warning(f"channel_update.validate() empty channel name in update_id={update_id}")
        return False

    if (update_row['new_disappearing_time_ms'] is not None and
        update_row['new_disappearing_time_ms'] < 0):
        log.warning(f"channel_update.validate() negative disappearing_time in update_id={update_id}")
        return False

    return True


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project a channel update event into the database.

    Stores updates only; channel state is resolved at query time.

    Args:
        event_id: Event ID of the channel_update
        recorded_by: Peer perspective
        recorded_at: Timestamp when recorded
        db: Database connection

    Returns:
        event_id if successfully projected, None otherwise
    """
    log.debug(f"channel_update.project() projecting update_id={event_id}, seen_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get and unwrap event
    event_blob = store.get(event_id, unsafedb)
    if not event_blob:
        log.warning(f"channel_update.project() blob not found for update_id={event_id}")
        return None

    unwrapped, _ = crypto.unwrap(event_blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"channel_update.project() unwrap failed for update_id={event_id}")
        return None

    event_data = crypto.parse_json(unwrapped)

    # Verify signature
    from events.identity import peer_shared
    updated_by = event_data.get('updated_by')
    public_key = peer_shared.get_public_key(updated_by, recorded_by, db)

    if not crypto.verify_event(event_data, public_key):
        log.warning(f"channel_update.project() signature verification failed for update_id={event_id}")
        return None

    # Extract fields
    update_id = event_id
    channel_id = event_data.get('channel_id')
    global_count = event_data.get('global_count')
    new_channel_name = event_data.get('new_channel_name')
    new_disappearing_time_ms = event_data.get('new_disappearing_time_ms')
    created_at = event_data.get('created_at')

    # Validate fields from the event data
    # Check admin authorization
    if not _validate_admin(updated_by, recorded_by, db):
        log.warning(f"channel_update.project() updater {updated_by} is not admin")
        return None

    # Check channel exists
    channel_row = safedb.query_one(
        "SELECT channel_id FROM channels WHERE channel_id = ? AND recorded_by = ? LIMIT 1",
        (channel_id, recorded_by)
    )

    if not channel_row:
        log.warning(f"channel_update.project() channel not found for channel_id={channel_id}")
        return None

    # Validate field values
    if (new_channel_name is not None and
        not new_channel_name.strip()):
        log.warning(f"channel_update.project() empty channel name in update_id={update_id}")
        return None

    if (new_disappearing_time_ms is not None and
        new_disappearing_time_ms < 0):
        log.warning(f"channel_update.project() negative disappearing_time in update_id={update_id}")
        return None

    # Insert the update into the table
    safedb.execute(
        """INSERT OR IGNORE INTO channel_updates
           (update_id, channel_id, updated_by, global_count, new_channel_name,
            new_disappearing_time_ms, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (update_id, channel_id, updated_by, global_count, new_channel_name,
         new_disappearing_time_ms, created_at, recorded_by, recorded_at)
    )

    # Record as valid event
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (update_id, recorded_by)
    )

    return event_id
