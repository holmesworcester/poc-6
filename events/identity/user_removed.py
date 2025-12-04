"""User removed event type - marks a user as removed from a network."""

# Registry metadata
EVENT_TYPE = 'user_removed'
SHAREABLE = True  # User removal syncs across network
EPHEMERAL = False
PROJECTION_TABLE = None

from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


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
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get removed_by's user_id from peers_shared (user→peer relationship stored there)
    peer_row = safedb.query_one(
        "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (removed_by_peer_id, recorded_by)
    )
    if not peer_row or not peer_row['user_id']:
        return False

    removed_by_user_id = peer_row['user_id']

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
    safedb = create_safe_db(db, recorded_by=removed_by_local_peer_id)

    # Get removed user's name for the return value (before removal)
    user_row = safedb.query_one(
        """SELECT COALESCE(un.name, u.name) as name
           FROM users u
           LEFT JOIN user_names un ON u.user_id = un.user_id AND u.recorded_by = un.recorded_by
           WHERE u.user_id = ? AND u.recorded_by = ? LIMIT 1""",
        (removed_user_id, removed_by_local_peer_id)
    )
    removed_user_name = user_row['name'] if user_row else '???'

    # Validate authorization
    if not validate(removed_user_id, removed_by_peer_id, removed_by_local_peer_id, db):
        raise ValueError("Not authorized to remove this user")

    # Create event data
    event_data = {
        'type': 'user_removed',
        'removed_user_id': removed_user_id,
        'removed_by': removed_by_peer_id,
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
    from events.identity import network
    from events.group import group_member

    # Get network_id to find all_users group
    network_row = safedb.query_one(
        "SELECT network_id FROM networks WHERE recorded_by = ? LIMIT 1",
        (removed_by_local_peer_id,)
    )
    members = []
    if network_row:
        all_users_group_id = network.get_all_users_group_id(
            network_row['network_id'], removed_by_local_peer_id, db
        )
        if all_users_group_id:
            members = group_member.list_members(all_users_group_id, removed_by_local_peer_id, db)

    return {
        'event_id': event_id,
        'removed_user_name': removed_user_name,
        'members': members
    }


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project user_removed event to state.

    When a user is removed:
    1. Insert into removed_users table
    2. Cascade: Mark all peers of this user as removed (peers cannot sync)
    3. Rotate group keys: Create new keys for all groups the user was a member of

    Args:
        event_id: Event ID
        recorded_by: Peer perspective for scoped insertion
        recorded_at: When the event was recorded
        db: Database connection

    Returns:
        event_id if successful, None otherwise
    """
    # Fetch and parse event data from store
    blob = store.get(event_id, db)
    if not blob:
        log.warning(f"user_removed.project() blob not found for {event_id[:20]}...")
        return None

    event_data = crypto.parse_json(blob)

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafe_db = create_unsafe_db(db)

    removed_user_id = event_data.get('removed_user_id')
    removed_at = event_data.get('created_at')
    removed_by = event_data.get('removed_by')

    if not removed_user_id:
        return None

    # Insert into removed_users table (with recorded_by for scoping)
    safedb.execute(
        """INSERT OR IGNORE INTO removed_users (user_id, removed_at, removed_by, recorded_by)
           VALUES (?, ?, ?, ?)""",
        (removed_user_id, removed_at, removed_by, recorded_by)
    )

    # Cascade: Find all peers for this user from peers_shared and mark them as removed
    peers = safedb.query(
        "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ?",
        (removed_user_id, recorded_by)
    )

    for peer_row in peers:
        peer_shared_id = peer_row['peer_shared_id']
        # Mark peer as removed in device-wide table by peer_shared_id
        unsafe_db.execute(
            """INSERT OR IGNORE INTO removed_peers (peer_shared_id, removed_at, removed_by)
               VALUES (?, ?, ?)""",
            (peer_shared_id, removed_at, removed_by)
        )

        # DELETE ALL CONNECTIONS for this peer (enforcement mechanism)
        # Uses connection module API which handles per-peer connection removal
        from events.network import connection
        deleted_count = connection.remove_connections_for_peer(peer_shared_id, db)
        log.info(f"user_removed.project() deleted {deleted_count} connection(s) for peer {peer_shared_id[:20]}...")

    # Rotate group keys for all groups this user was a member of
    # This prevents the removed user from decrypting future messages
    _rotate_keys_for_removed_user(removed_user_id, recorded_by, removed_at, db)

    return event_id


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
    share_timestamp = t_ms + 1000  # Space out key creation from removal events

    for group_row in group_memberships:
        group_id = group_row['group_id']
        try:
            group_key.rotate_for_removal(
                group_id=group_id,
                peer_id=recorded_by,
                peer_shared_id=peer_shared_id,
                t_ms=share_timestamp,
                removed_user_id=removed_user_id,
                db=db
            )
            share_timestamp += 100  # Space out timestamps for multiple groups
            log.info(f"user_removed._rotate_keys_for_removed_user() rotated key for group {group_id[:20]}...")
        except Exception as e:
            log.warning(f"user_removed._rotate_keys_for_removed_user() failed to rotate key for group {group_id[:20]}...: {e}")
            # Continue rotating keys for other groups even if one fails
