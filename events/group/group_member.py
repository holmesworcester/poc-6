"""Group member event type (shareable, encrypted) - represents group membership."""

# Registry metadata
EVENT_TYPE = 'group_member'
SHAREABLE = True  # Memberships sync for group access control
EPHEMERAL = False
PROJECTION_TABLE = ('group_members', 'user_id')

from typing import Any
import logging
from core import crypto
from core import store
from events.group import group_key
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db
from core import queues

log = logging.getLogger(__name__)


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
    group = safedb.query_one(
        "SELECT signed_by, key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
        (group_id, peer_id)
    )
    if not group:
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
        # Get adder's user_id from peers_shared (user→peer relationship stored there)
        adder_user_row = safedb.query_one(
            "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
            (peer_shared_id, peer_id)
        )
        if adder_user_row and adder_user_row['user_id']:
            # Get network_id
            network_row = safedb.query_one(
                "SELECT network_id FROM networks WHERE recorded_by = ? LIMIT 1",
                (peer_id,)
            )
            if network_row:
                from events.identity import admin as admin_module
                admin_grant_id = admin_module.my_grant(adder_user_row['user_id'], network_row['network_id'], peer_id, db)

    if admin_grant_id:
        log.info(f"group_member.create() including admin_grant={admin_grant_id[:20]}...")
    else:
        log.warning(f"group_member.create() NO admin_grant found - event may fail projection on receivers!")

    # Build prior array for admin action chain
    from events.identity import admin_action
    prior = admin_action.build_prior(peer_shared_id, peer_id, db)
    if prior:
        log.info(f"group_member.create() including prior chain: {[p[:20] + '...' if p else None for p in prior]}")

    # Create event data
    event_data = {
        'type': 'group_member',
        'group_id': group_id,
        'user_id': user_id,
        'added_by': peer_shared_id,
        'signed_by': peer_shared_id,
        'created_at': t_ms
    }

    # Include explicit admin_grant dependency
    if admin_grant_id:
        event_data['admin_grant'] = admin_grant_id

    # Include prior for admin action chain
    if prior:
        event_data['prior'] = prior

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get group key for encryption
    key_data = group_key.get_key(group['key_id'], peer_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event with recorded wrapper and projection
    member_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_member.create() created member_id={member_id}")

    # Share group key with new member
    # Get the new member's peer_shared_id from peers_shared (user→peer is one-to-many)
    member_peer = safedb.query_one(
        "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ?",
        (user_id, peer_id)
    )

    if member_peer:
        from events.group import group_key_shared
        try:
            group_key_shared.create(
                key_id=group['key_id'],
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
                key_id=group['key_id'],
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


def project(member_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project group_member event into group_members table."""
    log.debug(f"group_member.project() projecting member_id={member_id}, seen_by={recorded_by}")

    # Use SafeDB for peer-scoped queries
    safedb = create_safe_db(db, recorded_by=recorded_by)
    # Use UnsafeDB for store access
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(member_id, unsafedb)
    if not blob:
        log.warning(f"group_member.project() blob not found for member_id={member_id}")
        return None

    # Unwrap (decrypt)
    unwrapped, _ = crypto.unwrap(blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"group_member.project() unwrap failed for member_id={member_id}")
        return None  # Already blocked by recorded.project() if keys missing

    # Parse JSON
    event_data = crypto.parse_json(unwrapped)

    # Verify signature - get public key from added_by peer_shared
    from events.identity import peer_shared
    added_by = event_data['added_by']
    public_key = peer_shared.get_public_key(added_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"group_member.project() signature verification FAILED for member_id={member_id}")
        return None  # Reject unsigned or invalid signature

    # Check if signer is removed and action is valid post-removal (DAG ancestry check)
    from events.identity import admin_action
    if not admin_action.is_valid_after_removal(member_id, added_by, recorded_by, db):
        log.warning(f"group_member.project() rejecting post-removal action from {added_by[:20]}...")
        return None

    # Note: group_id and user_id dependencies are checked by recorded.check_deps()
    # before dispatch. If we get here, they're guaranteed to be in valid_events,
    # which means their projection succeeded and table rows exist.

    # Check authorization via explicit admin_grant chain
    # Per spec: admin_grant is the explicit dependency that proves admin status
    admin_grant = event_data.get('admin_grant')
    if admin_grant:
        # Verify admin_grant references an admin event for the adder's user
        # Get adder's user_id from peers_shared (user→peer relationship stored there)
        adder_user_row = safedb.query_one(
            "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
            (added_by, recorded_by)
        )
        if adder_user_row and adder_user_row['user_id']:
            grant_row = safedb.query_one(
                "SELECT user_id FROM admins WHERE admin_id = ? AND recorded_by = ?",
                (admin_grant, recorded_by)
            )
            if grant_row and grant_row['user_id'] == adder_user_row['user_id']:
                log.info(f"group_member.project() admin_grant chain verified for adder {added_by[:20]}...")
            else:
                log.warning(f"group_member.project() admin_grant {admin_grant[:20]}... does not authorize adder {added_by[:20]}...")
                return None
        else:
            log.warning(f"group_member.project() adder user not found for {added_by[:20]}...")
            return None
    else:
        # All group_member events should have admin_grant
        log.warning(f"group_member.project() missing admin_grant - invalid event")
        return None

    # Insert into group_members table
    safedb.execute(
        """INSERT OR IGNORE INTO group_members
           (member_id, group_id, user_id, added_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            member_id,
            event_data['group_id'],
            event_data['user_id'],
            added_by,
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )

    # Record in admin_actions table for DAG tracking
    from events.identity import admin_action
    admin_action.record_action(
        action_id=member_id,
        action_type='group_member',
        peer_shared_id=added_by,
        prior=event_data.get('prior'),
        created_at=event_data['created_at'],
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        db=db
    )

    log.info(f"group_member.project() successfully projected member_id={member_id}")

    # Unblock events waiting for this membership
    # Use both the specific user's membership and the generic group membership
    dep_ids = [
        f"group_membership_{event_data['group_id']}_{event_data['user_id']}",
        member_id
    ]

    for dep_id in dep_ids:
        unblocked = queues.blocked.notify_event_valid(dep_id, recorded_by, safedb)
        if unblocked:
            log.info(f"group_member.project() unblocked {len(unblocked)} events for {dep_id}")

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
