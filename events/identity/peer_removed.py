"""Peer removed event type - marks a peer as removed from syncing."""

# Registry metadata
EVENT_TYPE = 'peer_removed'
SHAREABLE = True  # Peer removal syncs to stop syncing with removed peer
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)

# V2 Projector specification
# Note: peer_removed uses 'removed_by' as the signer field (not standard 'signed_by')
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'removed_by',  # Custom signer field
        'type_field': 'signer_type',  # Always 'peer_shared'
    },
    'requires': {
        # The signer's peer_shared must exist for signature verification
        'signer_peer_shared': {
            'source': 'table',
            'table': 'peers_shared',
            'key': 'peer_shared_id',
            'key_from': 'removed_by',
            'fields': ['peer_shared_id', 'public_key'],
        },
    },
    'optional': {
        # The removed peer's info (for user lookup during key rotation)
        'removed_peer': {
            'source': 'table',
            'table': 'peers_shared',
            'key': 'peer_shared_id',
            'key_from': 'removed_peer_shared_id',
            'fields': ['peer_shared_id', 'user_id'],
        },
    },
}


def validate(removed_peer_shared_id: str, removed_by_peer_shared_id: str, recorded_by: str, db: Any) -> bool:
    """Validate that removed_by has authorization to remove the peer.

    Authorization rule:
    - Only admins can remove peers

    This prevents non-admin peers from rotating group keys and excluding other members.

    Args:
        removed_peer_shared_id: peer_shared_id being removed
        removed_by_peer_shared_id: peer_shared_id removing the peer
        recorded_by: Local peer ID for database lookups
        db: Database connection

    Returns:
        True if authorized (is admin), False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Check if removed_by is an admin
    # Use centralized is_admin() function which handles both normal admins and first_peer
    from events.identity import invite
    return invite.is_admin(removed_by_peer_shared_id, recorded_by, db)


def create(removed_peer_shared_id: str, removed_by_peer_shared_id: str, removed_by_local_peer_id: str, t_ms: int, db: Any) -> str:
    """Create a peer_removed event.

    Args:
        removed_peer_shared_id: peer_shared_id to remove
        removed_by_peer_shared_id: peer_shared_id of remover
        removed_by_local_peer_id: Local peer ID of remover (for signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        event_id of the created peer_removed event
    """
    # Validate authorization
    if not validate(removed_peer_shared_id, removed_by_peer_shared_id, removed_by_local_peer_id, db):
        raise ValueError("Not authorized to remove this peer")

    # Create event data
    event_data = {
        'type': 'peer_removed',
        'removed_peer_shared_id': removed_peer_shared_id,
        'removed_by': removed_by_peer_shared_id,
        'signer_type': 'peer_shared',  # For v2 resolver
        'created_at': t_ms
    }

    # Sign the event with remover's private key
    from events.identity import peer
    private_key = peer.get_private_key(removed_by_local_peer_id, removed_by_local_peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext (no inner encryption)
    # store.event() creates recorded wrapper and triggers projection
    blob = crypto.canonicalize_json(signed_event)
    event_id = store.event(blob, removed_by_local_peer_id, t_ms, db)

    return event_id


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for peer_removed - returns writes without side effects.

    Core projection: insert into removed_peers table.
    Side effects (connection removal, key rotation) are handled in project() wrapper.
    """
    event_data = ctx.event_data

    removed_peer_shared_id = event_data.get('removed_peer_shared_id')
    removed_at = event_data.get('created_at')
    signed_by = event_data.get('removed_by')

    if not removed_peer_shared_id:
        log.warning(f"peer_removed.project_pure() missing removed_peer_shared_id")
        return ProjectorResult(writes=tuple(), valid_event=False)

    if not signed_by:
        log.warning(f"peer_removed.project_pure() missing removed_by field")
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Core write: insert into removed_peers table
    # Note: removed_peers is device-wide (no recorded_by column)
    writes = [
        WriteOp(
            op='insert',
            table='removed_peers',
            values={
                'peer_shared_id': removed_peer_shared_id,
                'removed_at': removed_at,
                'signed_by': signed_by,
            },
        ),
    ]

    return ProjectorResult(writes=tuple(writes), valid_event=True)


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project peer_removed event to state (legacy wrapper).

    NOTE: When EVENT_SPEC and project_pure are defined, recorded.py uses the v2 path
    directly and this function is NOT called. Side effects (connection removal, key
    rotation) are handled in recorded.py's post-projection hook for peer_removed.

    This legacy wrapper exists for backwards compatibility if called directly.
    """
    from core.projection_v2.resolver import resolve
    from core.projection_v2.apply import apply_result

    resolve_result = resolve(EVENT_SPEC, event_id, recorded_by, recorded_at, db)

    if resolve_result.status == 'block':
        log.debug(f"peer_removed.project() blocked on deps: {resolve_result.missing}")
        return None
    if resolve_result.status == 'reject':
        log.warning(f"peer_removed.project() rejected: {resolve_result.error}")
        return None

    result = project_pure(resolve_result.ctx)
    if not result.valid_event:
        return None

    apply_result(result, recorded_by, db)

    # Side effects are handled in recorded.py for v2 path
    # If called directly (legacy), we need to handle them here
    removed_peer_shared_id = resolve_result.ctx.event_data.get('removed_peer_shared_id')
    if removed_peer_shared_id:
        from events.network import connection_request
        connection_request.remove_connections_for_peer(removed_peer_shared_id, db)

        safedb = create_safe_db(db, recorded_by=recorded_by)
        peer_row = safedb.query_one(
            "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
            (removed_peer_shared_id, recorded_by)
        )
        if peer_row and peer_row.get('user_id'):
            _rotate_keys_for_removed_peer_user(peer_row['user_id'], recorded_by, recorded_at, db)

    return event_id


def _rotate_keys_for_removed_peer_user(removed_user_id: str, recorded_by: str, t_ms: int, db: Any) -> None:
    """Rotate group keys if this was the last peer of a removed user.

    This ensures the removed user (via their last peer) cannot decrypt future messages.

    Args:
        removed_user_id: User ID whose last peer was removed
        recorded_by: Peer ID performing the removal
        t_ms: Timestamp for key creation
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Find all groups this user was a member of
    group_memberships = safedb.query(
        "SELECT DISTINCT group_id FROM group_members WHERE user_id = ? AND recorded_by = ?",
        (removed_user_id, recorded_by)
    )

    if not group_memberships:
        log.info(f"peer_removed._rotate_keys_for_removed_peer_user() user {removed_user_id[:20]}... was not a member of any groups")
        return

    log.info(f"peer_removed._rotate_keys_for_removed_peer_user() rotating keys for {len(group_memberships)} groups")

    # Get remover's peer_shared_id for signing rotated keys
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (recorded_by, recorded_by)
    )
    if not peer_self_row:
        log.warning(f"peer_removed._rotate_keys_for_removed_peer_user() could not find peer_shared_id for {recorded_by[:20]}..., skipping key rotation")
        return

    peer_shared_id = peer_self_row['peer_shared_id']

    # Rotate key for each group
    from events.group import group_key

    for group_row in group_memberships:
        group_id = group_row['group_id']
        try:
            group_key.rotate_for_removal(
                group_id=group_id,
                peer_id=recorded_by,
                peer_shared_id=peer_shared_id,
                t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                removed_user_id=removed_user_id,
                db=db
            )
            log.info(f"peer_removed._rotate_keys_for_removed_peer_user() rotated key for group {group_id[:20]}...")
        except Exception as e:
            log.warning(f"peer_removed._rotate_keys_for_removed_peer_user() failed to rotate key for group {group_id[:20]}...: {e}")
            # Continue rotating keys for other groups even if one fails
