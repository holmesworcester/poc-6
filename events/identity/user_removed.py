"""User removed event type - marks a user as removed from a network."""

# Registry metadata
EVENT_TYPE = 'user_removed'
SHAREABLE = True  # User removal syncs across network
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp, Command
from core.projection_v2.apply import register_command_handler
from events.identity import user, peer_shared, network

log = logging.getLogger(__name__)

# V2 Projector specification
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'removed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'signer_peer_shared': {
            'source': 'table',
            'table': 'peers_shared',
            'key': 'peer_shared_id',
            'key_from': 'removed_by',
            'fields': ['peer_shared_id', 'public_key'],
        },
    },
    'optional': {},
}


def validate(removed_user_id: str, removed_by_peer_id: str, recorded_by: str, db: Any) -> bool:
    """Validate that removed_by has authorization to remove the user.

    Authorization rule:
    - User can remove themselves (via any linked peer)
    - Admin can remove any user

    Args:
        removed_user_id: User ID being removed
        removed_by_peer_id: peer_shared_id removing the user
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if authorized, False otherwise
    """
    # Get removed_by's user_id
    removed_by_user_id = peer_shared.get_user_id(removed_by_peer_id, recorded_by, db)
    if not removed_by_user_id:
        return False

    # Rule 1: User can remove themselves
    if removed_by_user_id == removed_user_id:
        return True

    # Rule 2: Admin can remove any user
    # Use centralized is_admin() function which handles both normal admins and first_peer
    from events.identity import invite
    return invite.is_admin(removed_by_peer_id, recorded_by, db)


def create(removed_user_id: str, removed_by_peer_id: str, removed_by_local_peer_id: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Create a user_removed event.

    Args:
        removed_user_id: User ID to remove
        removed_by_peer_id: peer_shared_id of remover (for event data)
        removed_by_local_peer_id: Local peer ID of remover (for signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        dict with:
        - event_id: ID of the created user_removed event
        - removed_user_name: Name of the removed user (for display)
        - members: Updated list of group members (excluding the removed user)
    """
    # Get removed user's name for the return value (before removal)
    removed_user_name = user.get_display_name(removed_user_id, removed_by_local_peer_id, db) or '???'

    # Validate authorization
    if not validate(removed_user_id, removed_by_peer_id, removed_by_local_peer_id, db):
        raise ValueError("Not authorized to remove this user")

    # Create event data
    event_data = {
        'type': 'user_removed',
        'removed_user_id': removed_user_id,
        'removed_by': removed_by_peer_id,
        'signer_type': 'peer_shared',
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

    # Get updated member list after removal (for immediate UI feedback)
    from events.group import group_member

    # Get network_id to find all_users group
    network_id = network.get_network_id(removed_by_local_peer_id, db)
    members = []
    if network_id:
        all_users_group_id = network.get_all_users_group_id(
            network_id, removed_by_local_peer_id, db
        )
        if all_users_group_id:
            members = group_member.list_members(all_users_group_id, removed_by_local_peer_id, db)

    return {
        'event_id': event_id,
        'removed_user_name': removed_user_name,
        'members': members
    }


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for user_removed - returns writes without side effects.

    Core projection: insert into removed_users table.
    Side effects (peer cascade, connection removal, key rotation) handled in recorded.py.
    """
    event_data = ctx.event_data
    recorded_by = ctx.recorded_by

    removed_user_id = event_data.get('removed_user_id')
    removed_at = event_data.get('created_at')
    signed_by = event_data.get('removed_by')

    if not removed_user_id:
        log.warning(f"user_removed.project_pure() missing removed_user_id")
        return ProjectorResult(writes=tuple(), valid_event=False)

    if not signed_by:
        log.warning(f"user_removed.project_pure() missing removed_by field")
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Core write: insert into removed_users table (scoped by recorded_by)
    writes = [
        WriteOp(
            op='insert',
            table='removed_users',
            values={
                'user_id': removed_user_id,
                'removed_at': removed_at,
                'signed_by': signed_by,
                'recorded_by': recorded_by,
            },
        ),
    ]

    # Side effects: cascade peer removal, delete connections, rotate keys
    commands = (
        Command(
            command_type='handle_user_removed_side_effects',
            args={
                'removed_user_id': removed_user_id,
                'removed_at': removed_at,
                'removed_by': signed_by,
            }
        ),
    )

    return ProjectorResult(writes=tuple(writes), valid_event=True, commands=commands)


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project user_removed event to state (legacy wrapper).

    NOTE: When EVENT_SPEC and project_pure are defined, recorded.py uses the v2 path
    directly and this function is NOT called. Side effects are handled in recorded.py.

    This legacy wrapper exists for backwards compatibility if called directly.
    """
    from core.projection_v2.resolver import resolve
    from core.projection_v2.apply import apply_result

    resolve_result = resolve(EVENT_SPEC, event_id, recorded_by, recorded_at, db)

    if resolve_result.status == 'block':
        log.debug(f"user_removed.project() blocked on deps: {resolve_result.missing}")
        return None
    if resolve_result.status == 'reject':
        log.warning(f"user_removed.project() rejected: {resolve_result.error}")
        return None

    result = project_pure(resolve_result.ctx)
    if not result.valid_event:
        return None

    apply_result(result, recorded_by, db)

    # Side effects are handled in recorded.py for v2 path
    # If called directly (legacy), we need to handle them here
    event_data = resolve_result.ctx.event_data
    removed_user_id = event_data.get('removed_user_id')
    removed_at = event_data.get('created_at')
    removed_by = event_data.get('removed_by')

    if removed_user_id:
        _handle_user_removed_side_effects(
            removed_user_id, removed_at, removed_by, recorded_by, db
        )

    return event_id


def _handle_user_removed_side_effects(
    removed_user_id: str, removed_at: int, removed_by: str, recorded_by: str, db: Any
) -> None:
    """Handle side effects of user removal.

    This is called both from the legacy project() wrapper and from recorded.py's
    post-projection hook for the v2 path.

    Side effects:
    1. Cascade: Mark all peers of the removed user as removed
    2. Delete connections for each of those peers
    3. Rotate group keys (only if this peer created the event)
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafe_db = create_unsafe_db(db)

    # Cascade: Find all peers for this user from peers_shared and mark them as removed
    peers = safedb.query(
        "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ?",
        (removed_user_id, recorded_by)
    )

    for peer_row in peers:
        peer_shared_id = peer_row['peer_shared_id']
        # Mark peer as removed in device-wide table by peer_shared_id
        unsafe_db.execute(
            """INSERT OR IGNORE INTO removed_peers (peer_shared_id, removed_at, signed_by)
               VALUES (?, ?, ?)""",
            (peer_shared_id, removed_at, removed_by)
        )

        # DELETE ALL CONNECTIONS for this peer (enforcement mechanism)
        from events.network import connection_request
        deleted_count = connection_request.remove_connections_for_peer(peer_shared_id, db)
        log.info(f"user_removed: deleted {deleted_count} connection(s) for peer {peer_shared_id[:20]}...")

    # Rotate group keys for all groups this user was a member of
    # IMPORTANT: Only the peer who CREATED this event should rotate keys.
    # Other peers receive the rotated keys via group_key_shared.
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (recorded_by, recorded_by)
    )
    if peer_self_row and peer_self_row['peer_shared_id'] == removed_by:
        # This peer created the user_removed event - they should rotate keys
        _rotate_keys_for_removed_user(removed_user_id, recorded_by, removed_at, db)
    else:
        log.debug(f"user_removed: skipping key rotation - not the event creator")


def _rotate_keys_for_removed_user(removed_user_id: str, recorded_by: str, t_ms: int, db: Any) -> None:
    """Rotate group keys for all groups a removed user was a member of.

    This ensures the removed user cannot decrypt future messages in any group.

    Args:
        removed_user_id: User ID being removed
        recorded_by: Peer ID performing the removal (and key rotation)
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
        log.info(f"user_removed._rotate_keys_for_removed_user() user {removed_user_id[:20]}... was not a member of any groups")
        return

    log.info(f"user_removed._rotate_keys_for_removed_user() rotating keys for {len(group_memberships)} groups")

    # Get remover's peer_shared_id for signing rotated keys
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (recorded_by, recorded_by)
    )
    if not peer_self_row:
        log.warning(f"user_removed._rotate_keys_for_removed_user() could not find peer_shared_id for {recorded_by[:20]}..., skipping key rotation")
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
            log.info(f"user_removed._rotate_keys_for_removed_user() rotated key for group {group_id[:20]}...")
        except Exception as e:
            log.warning(f"user_removed._rotate_keys_for_removed_user() failed to rotate key for group {group_id[:20]}...: {e}")
            # Continue rotating keys for other groups even if one fails


def _command_handle_user_removed_side_effects(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Command handler wrapper for _handle_user_removed_side_effects."""
    removed_user_id = args.get('removed_user_id')
    removed_at = args.get('removed_at')
    removed_by = args.get('removed_by')

    if not removed_user_id:
        log.warning("handle_user_removed_side_effects: missing removed_user_id")
        return

    _handle_user_removed_side_effects(removed_user_id, removed_at, removed_by, recorded_by, db)


# Register command handler at module load
register_command_handler('handle_user_removed_side_effects', _command_handle_user_removed_side_effects)
