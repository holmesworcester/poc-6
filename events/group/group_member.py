"""Group member event type (shareable, signed) - represents group membership.

In sender key model, group_member events are signed but not encrypted.
Membership metadata is public; messages are encrypted with sender keys.
"""

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
from core.projection.types import ProjectorResult, WriteOp, Command
from core.projection.apply import register_command_handler

log = logging.getLogger(__name__)


# v2 event specification - signed by peer_shared, unencrypted in sender key model
EVENT_SPEC = {
    'encrypted': False,  # Sender key model: membership metadata is public
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

    # Command to distribute our sender keys to the new member
    commands = (
        Command(
            command_type='distribute_sender_keys_to_new_member',
            args={
                'group_id': group_id,
                'new_member_user_id': user_id,
            }
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True, commands=commands)


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
    blob = wire_format.encode_group_member_wire_event(
        group_id_b64=group_id,
        user_id_b64=user_id,
        added_by_b64=peer_shared_id,
        admin_grant_b64=admin_grant_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )
    member_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_member.create() created member_id={member_id}")

    # NOTE: In sender key model, group keys are not shared here.
    # Sender keys are distributed via TreeKEM to the new member.
    # The new member will receive sender keys from existing senders
    # as those senders continue sending messages to the group.

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


def _handle_distribute_sender_keys_to_new_member(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Distribute our sender keys to a newly visible group member.

    Called when a group_member event is projected. This ensures that when
    we see a new member join (via sync), we share our existing sender keys
    with them so they can decrypt our messages.
    """
    from events.group import sender_key, pubkey_shared

    group_id = args['group_id']
    new_member_user_id = args['new_member_user_id']

    # Unwrap db if needed
    raw_db = db._db if hasattr(db, '_db') else db
    safedb = create_safe_db(raw_db, recorded_by=recorded_by)

    # Get our peer_shared_id
    peer_self = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (recorded_by, recorded_by)
    )
    if not peer_self:
        return

    our_peer_shared_id = peer_self['peer_shared_id']

    # Get the new member's peer_shared_id
    new_member_peer_shared = safedb.query_one(
        "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ?",
        (new_member_user_id, recorded_by)
    )
    if not new_member_peer_shared:
        log.debug(f"distribute_sender_keys: no peer_shared for new member {new_member_user_id[:20]}...")
        return

    new_member_peer_shared_id = new_member_peer_shared['peer_shared_id']

    # Don't distribute to ourselves
    if new_member_peer_shared_id == our_peer_shared_id:
        return

    # SECURITY GATE: Don't distribute keys to removed users
    # This prevents leaking keys to users who were removed (even if membership event arrives late)
    is_removed = safedb.query_one(
        "SELECT 1 FROM removed_users WHERE user_id = ? AND recorded_by = ?",
        (new_member_user_id, recorded_by)
    )
    if is_removed:
        log.info(f"distribute_sender_keys: BLOCKED - user {new_member_user_id[:20]}... was removed, not sharing keys")
        return

    # Get all our sender keys for this group
    our_keys = safedb.query(
        """SELECT DISTINCT ka.key_id FROM key_announces ka
           INNER JOIN secrets s ON s.secret_id = ka.key_id AND s.recorded_by = ka.recorded_by
           WHERE ka.group_id = ? AND ka.signed_by = ? AND ka.recorded_by = ?""",
        (group_id, our_peer_shared_id, recorded_by)
    )

    if not our_keys:
        log.debug(f"distribute_sender_keys: no keys to distribute for group {group_id[:20]}...")
        return

    # Get the new member's pubkey
    new_member_pubkey = pubkey_shared.get_pubkey_for_peer(new_member_peer_shared_id, recorded_by, raw_db)
    if not new_member_pubkey:
        log.warning(f"distribute_sender_keys: no pubkey for new member {new_member_peer_shared_id[:20]}...")
        return

    from events.group import secret_shared
    from core import crypto

    recipient_pubkey_id = crypto.b64encode(new_member_pubkey['id'])

    log.info(f"distribute_sender_keys: sharing {len(our_keys)} keys to new member {new_member_peer_shared_id[:20]}...")

    for i, key_row in enumerate(our_keys):
        key_id = key_row['key_id']
        try:
            secret_shared.create_to_pubkey(
                secret_id=key_id,
                peer_id=recorded_by,
                peer_shared_id=our_peer_shared_id,
                recipient_pubkey_id=recipient_pubkey_id,
                removal_epoch_id=None,
                t_ms=recorded_at + i,
                db=raw_db,
            )
            log.debug(f"distribute_sender_keys: shared key {key_id[:20]}... to {new_member_peer_shared_id[:20]}...")
        except Exception as e:
            log.warning(f"distribute_sender_keys: failed to share {key_id[:20]}...: {e}")


# Register the command handler
register_command_handler('distribute_sender_keys_to_new_member', _handle_distribute_sender_keys_to_new_member)
