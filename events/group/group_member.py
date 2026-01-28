"""Group member event type (shareable, encrypted) - represents group membership."""

# Registry metadata
EVENT_TYPE = 'group_member'
SHAREABLE = True  # Memberships sync for group access control
PROJECTION_TABLE = ('group_members', 'user_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from events.group import group
from events.identity import peer_shared, network, peer
from core.db import create_safe_db, create_unsafe_db
from core import queues
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# event specification - signed by peer_shared, encrypted
EVENT_SPEC = {
    'encrypted': True,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'group': {
            'source': 'table',
            'table': 'groups',
            'key': 'group_id',
            'fields': ['group_id'],
        },
        'user': {
            'source': 'table',
            'table': 'users',
            'key': 'user_id',
            'fields': ['user_id'],
        },
        'adder': {
            'source': 'table',
            'table': 'peers_shared',
            'key': 'peer_shared_id',
            'key_from': 'added_by',
            'fields': ['user_id'],
        },
        'admin_grant': {
            'source': 'table',
            'table': 'admins',
            'key': 'admin_id',
            'key_from': 'admin_grant',
            'fields': ['user_id'],
        },
    },
    'optional': {},
    'cascade_on_delete': [],
}

def validate(group_id: str, added_by: str, recorded_by: str, db: Any) -> bool:
    """Validate that added_by has authorization to add members to the group.

    Authorization rule:
    - added_by must have an admin event in the admins table

    Uses centralized is_admin() function from events.identity.invite.

    Args:
        group_id: Group to check membership for
        added_by: peer_shared_id attempting to add members
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if authorized, False otherwise
    """
    from events.identity import invite
    return invite.is_admin(added_by, recorded_by, db)


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for group_member events."""
    event_data = ctx.event_data

    group_id = event_data.get('group_id')
    user_id = event_data.get('user_id')
    added_by = event_data.get('added_by')
    signed_by = event_data.get('signed_by')
    admin_grant = event_data.get('admin_grant')
    created_at = event_data.get('created_at')

    if not all([group_id, user_id, added_by, signed_by, admin_grant, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_group_member(group_id, user_id, added_by, admin_grant)

    if added_by != signed_by:
        return ProjectorResult(writes=tuple(), valid_event=False)

    adder_row = ctx.deps.get('adder') or {}
    admin_row = ctx.deps.get('admin_grant') or {}
    adder_user_id = adder_row.get('user_id')
    admin_user_id = admin_row.get('user_id')

    if not adder_user_id or not admin_user_id or adder_user_id != admin_user_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='group_members',
            values={
                'member_id': ctx.event_id,
                'group_id': group_id,
                'user_id': user_id,
                'added_by': added_by,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_group_member(
    group_id: str,
    user_id: str,
    added_by: str,
    admin_grant: str | None,
) -> None:
    """Validate group_member fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_group_member_plaintext(
        group_id=crypto.b64decode(group_id),
        user_id=crypto.b64decode(user_id),
        added_by=crypto.b64decode(added_by),
        admin_grant_id=crypto.b64decode(admin_grant) if admin_grant else None,
    )
    decoded = wire_format.decode_group_member_plaintext(plaintext)
    if decoded["group_id"] != crypto.b64decode(group_id):
        raise ValueError("wire shadow decode group_id mismatch")


def create(group_id: str, user_id: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any,
           admin_grant: str | None = None) -> str:
    """Create a group_member event to add a user to a group.

    Only admins can add new members to groups.
    Automatically shares group key with the new member.

    Args:
        group_id: Group to add member to
        user_id: User (peer_shared_id) to add to the group
        peer_id: Local peer ID (for signing and seeing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection
        admin_grant: Optional admin_id that grants authority to add members.
                    If provided, used directly. If None, looked up from admins table.

    Returns:
        member_id: The stored group_member event ID
    """
    log.info(f"group_member.create() adding user={user_id} to group={group_id} by {peer_shared_id}")

    # Use SafeDB for peer-scoped queries
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Validate group exists
    group_row = group.get(group_id, peer_id, db)
    if not group_row:
        raise ValueError(f"Group {group_id} not found")

    # Check authorization - caller must be admin to add members
    if not validate(group_id, peer_shared_id, peer_id, db):
        raise ValueError(f"User {peer_shared_id} not authorized to add members to group {group_id} (only admins can add members)")

    # Get admin_grant for the adding user (explicit dependency for convergence)
    # This is REQUIRED for events that need admin authorization to project correctly
    # when replayed in different orders on receiving peers.
    admin_grant_id = admin_grant  # Use passed-in value if provided

    if not admin_grant_id:
        # Look up admin_grant from admins table
        # Get adder's user_id
        adder_user_id = peer_shared.get_user_id(peer_shared_id, peer_id, db)
        if adder_user_id:
            # Get network_id
            network_id = network.get_network_id(peer_id, db)
            if network_id:
                from events.identity import admin as admin_module
                admin_grant_id = admin_module.my_grant(adder_user_id, network_id, peer_id, db)

    if admin_grant_id:
        log.info(f"group_member.create() including admin_grant={admin_grant_id[:20]}...")
    else:
        log.warning(f"group_member.create() NO admin_grant found - event may fail projection on receivers!")

    _wire_shadow_group_member(group_id, user_id, peer_shared_id, admin_grant_id)

    private_key = peer.get_private_key(peer_id, peer_id, db)
    key_data = group.pick_key(group_id, peer_id, db)
    blob = wire_format.encode_group_member_wire_event(
        group_id_b64=group_id,
        user_id_b64=user_id,
        added_by_b64=peer_shared_id,
        admin_grant_b64=admin_grant_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )
    member_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_member.create() created member_id={member_id}")

    # Share group key with new member
    # Get the new member's peer_shared_id from peers_shared (user→peer is one-to-many)
    member_peer = peer_shared.get_for_user(user_id, peer_id, db)

    if member_peer:
        from events.group import group_key_shared
        try:
            group_key_shared.create(
                key_id=group_row['key_id'],
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_peer_id=member_peer['peer_shared_id'],
                t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                db=db
            )
            log.info(f"group_member.create() shared key with new member {user_id}")
        except Exception as e:
            log.warning(f"group_member.create() failed to share key with {user_id}: {e}")

    # Per design doc: Share key to all active device links for this user
    # Any new group memberships must be sealed to all active device links
    # This ensures all devices of a user can decrypt groups they're added to
    from events.group import group_key_shared

    # Get all active device links for this user (peer_shared entries with matching user_id)
    other_devices = safedb.query(
        """SELECT peer_shared_id FROM peers_shared
           WHERE user_id = ? AND recorded_by = ? AND peer_shared_id != ?""",
        (user_id, peer_id, member_peer['peer_shared_id'] if member_peer else None)
    )

    for device_row in other_devices:
        other_peer_shared_id = device_row['peer_shared_id']
        try:
            # Share key with other device by creating group_key_shared sealed to their identity
            group_key_shared.create(
                key_id=group_row['key_id'],
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_peer_id=other_peer_shared_id,
                t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                db=db
            )
            log.info(f"group_member.create() shared key to device link {other_peer_shared_id[:20]}...")
        except Exception as e:
            log.warning(f"group_member.create() failed to share key to device link {other_peer_shared_id[:20]}...: {e}")

    return member_id


def is_member(user_id: str, group_id: str, recorded_by: str, db: Any) -> bool:
    """Check if a user is a member of a group.

    Args:
        user_id: User's ID to check
        group_id: Group to check membership in
        recorded_by: Perspective of which peer is checking
        db: Database connection

    Returns:
        True if user is a member, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Check if user is a member of the group
    member = safedb.query_one(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ? AND recorded_by = ?",
        (group_id, user_id, recorded_by)
    )

    return member is not None


def list_members(group_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all members of a group (excluding removed users).

    Args:
        group_id: Group to list members for
        recorded_by: Perspective of which peer is querying
        db: Database connection

    Returns:
        List of member dicts with user_id, name, added_by, created_at
        (name is preferentially from user_names table (encrypted username), falls back to users.name)
        Removed users are filtered out.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Query group_members table with user names joined
    # Prefer encrypted username from user_names table if available, else fall back to plaintext from users table
    # Filter out removed users by LEFT JOIN on removed_users and checking for NULL
    return safedb.query(
        """SELECT gm.user_id, COALESCE(un.name, u.name) as name, gm.added_by, gm.created_at
           FROM group_members gm
           JOIN users u ON gm.user_id = u.user_id AND gm.recorded_by = u.recorded_by
           LEFT JOIN user_names un ON gm.user_id = un.user_id AND gm.recorded_by = un.recorded_by
           LEFT JOIN removed_users ru ON gm.user_id = ru.user_id AND gm.recorded_by = ru.recorded_by
           WHERE gm.group_id = ? AND gm.recorded_by = ? AND ru.user_id IS NULL
           ORDER BY gm.created_at ASC""",
        (group_id, recorded_by)
    )
