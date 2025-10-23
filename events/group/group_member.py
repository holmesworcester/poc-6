"""Group member event type (shareable, encrypted) - represents group membership."""
from typing import Any
import logging
import crypto
import store
from events.group import group_key
from events.identity import peer
from db import create_safe_db, create_unsafe_db
import queues

log = logging.getLogger(__name__)


def create(group_id: str, user_id: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any) -> str:
    """Create a group_member event to add a user to a group.

    Only group creator or existing members can add new members (hierarchy-free).
    Automatically shares group key with the new member.

    Args:
        group_id: Group to add member to
        user_id: User (peer_shared_id) to add to the group
        peer_id: Local peer ID (for signing and seeing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        member_id: The stored group_member event ID
    """
    log.info(f"group_member.create() adding user={user_id} to group={group_id} by {peer_shared_id}")

    # Use SafeDB for peer-scoped queries
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Validate group exists
    group = safedb.query_one(
        "SELECT created_by, key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
        (group_id, peer_id)
    )
    if not group:
        raise ValueError(f"Group {group_id} not found")

    # Check permission: creator OR existing member
    if peer_shared_id != group['created_by']:
        member = safedb.query_one(
            "SELECT 1 FROM group_members_wip WHERE group_id = ? AND user_id = ? AND recorded_by = ?",
            (group_id, peer_shared_id, peer_id)
        )
        if not member:
            raise ValueError(f"User {peer_shared_id} not authorized to add members to group {group_id}")

    # Create event data
    event_data = {
        'type': 'group_member',
        'group_id': group_id,
        'user_id': user_id,
        'added_by': peer_shared_id,
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

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
    # Get the new member's peer_id from users table
    member_peer = safedb.query_one(
        "SELECT peer_id FROM users WHERE user_id = ? AND recorded_by = ?",
        (user_id, peer_id)
    )

    if member_peer:
        from events.group import group_key_shared
        try:
            group_key_shared.create(
                key_id=group['key_id'],
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_peer_id=member_peer['peer_id'],
                t_ms=t_ms + 1,
                db=db
            )
            log.info(f"group_member.create() shared key with new member {user_id}")
        except Exception as e:
            log.warning(f"group_member.create() failed to share key with {user_id}: {e}")

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

    # Check if group exists
    group = safedb.query_one(
        "SELECT created_by FROM groups WHERE group_id = ? AND recorded_by = ?",
        (event_data['group_id'], recorded_by)
    )

    if not group:
        log.info(f"group_member.project() blocking: group {event_data['group_id']} not found")
        # Don't block here - let recorded.project() handle blocking with recorded_id
        return None

    # Check if user being added exists
    user = safedb.query_one(
        "SELECT 1 FROM users WHERE user_id = ? AND recorded_by = ?",
        (event_data['user_id'], recorded_by)
    )

    if not user:
        log.info(f"group_member.project() blocking: user {event_data['user_id']} not found")
        # Don't block here - let recorded.project() handle blocking with recorded_id
        return None

    # Check authorization: added_by == creator OR added_by is member
    authorized = False

    if added_by == group['created_by']:
        # Bootstrap case: group creator can add members
        authorized = True
        log.debug(f"group_member.project() authorized: {added_by} is group creator")
    else:
        # Check if added_by is an existing member
        member = safedb.query_one(
            "SELECT 1 FROM group_members_wip WHERE group_id = ? AND user_id = ? AND recorded_by = ?",
            (event_data['group_id'], added_by, recorded_by)
        )
        authorized = member is not None

        if authorized:
            log.debug(f"group_member.project() authorized: {added_by} is existing member")
        else:
            log.info(f"group_member.project() not authorized: {added_by} is not creator or member")

    if not authorized:
        # Block on missing membership
        log.info(f"group_member.project() blocking on missing membership")
        # Don't block here - let recorded.project() handle blocking with recorded_id
        return None

    # Insert into group_members_wip table
    safedb.execute(
        """INSERT OR IGNORE INTO group_members_wip
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


def is_member(peer_shared_id: str, group_id: str, recorded_by: str, db: Any) -> bool:
    """Check if a user is a member of a group.

    Args:
        peer_shared_id: User's peer_shared_id to check
        group_id: Group to check membership in
        recorded_by: Perspective of which peer is checking
        db: Database connection

    Returns:
        True if user is a member, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    member = safedb.query_one(
        "SELECT 1 FROM group_members_wip WHERE group_id = ? AND user_id = ? AND recorded_by = ?",
        (group_id, peer_shared_id, recorded_by)
    )

    return member is not None


def list_members(group_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all members of a group.

    Args:
        group_id: Group to list members for
        recorded_by: Perspective of which peer is querying
        db: Database connection

    Returns:
        List of member dicts with user_id, added_by, created_at
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    return safedb.query(
        """SELECT user_id, added_by, created_at
           FROM group_members_wip
           WHERE group_id = ? AND recorded_by = ?
           ORDER BY created_at ASC""",
        (group_id, recorded_by)
    )
