"""User removed event type - marks a user as removed from a network."""
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

    # Get removed_by's user_id (map peer_shared_id to user_id)
    user_row = safedb.query_one(
        "SELECT user_id FROM users WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (removed_by_peer_id, recorded_by)
    )
    if not user_row:
        return False

    removed_by_user_id = user_row['user_id']

    # Rule 1: User can remove themselves
    if removed_by_user_id == removed_user_id:
        return True

    # Rule 2: Admin can remove any user
    # Use centralized is_admin() function which handles both normal admins and first_peer
    from events.identity import invite
    return invite.is_admin(removed_by_peer_id, recorded_by, db)


def create(removed_user_id: str, removed_by_peer_id: str, removed_by_local_peer_id: str, t_ms: int, db: Any) -> str:
    """Create a user_removed event.

    Args:
        removed_user_id: User ID to remove
        removed_by_peer_id: peer_shared_id of remover (for event data)
        removed_by_local_peer_id: Local peer ID of remover (for signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        event_id of the created user_removed event
    """
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
    blob = crypto.canonicalize_json(signed_event)
    event_id = store.event(blob, removed_by_local_peer_id, t_ms, db)

    # Project to database state
    project(event_id, signed_event, recorded_by=removed_by_local_peer_id, db=db)

    return event_id


def project(event_id: str, event_data: dict, recorded_by: str, db: Any) -> None:
    """Project user_removed event to state.

    When a user is removed:
    1. Insert into removed_users table
    2. Cascade: Mark all peers of this user as removed (peers cannot sync)
    3. Rotate group keys: Create new keys for all groups the user was a member of

    Args:
        event_id: Event ID
        event_data: Event data dictionary
        recorded_by: Peer perspective for scoped insertion
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafe_db = create_unsafe_db(db)

    removed_user_id = event_data.get('removed_user_id')
    removed_at = event_data.get('created_at')
    removed_by = event_data.get('removed_by')

    if not removed_user_id:
        return

    # Insert into removed_users table (with recorded_by for scoping)
    safedb.execute(
        """INSERT OR IGNORE INTO removed_users (user_id, removed_at, removed_by, recorded_by)
           VALUES (?, ?, ?, ?)""",
        (removed_user_id, removed_at, removed_by, recorded_by)
    )

    # Cascade: Find all peers for this user and mark them as removed by peer_shared_id
    peers = safedb.query(
        "SELECT peer_id FROM users WHERE user_id = ? AND recorded_by = ?",
        (removed_user_id, recorded_by)
    )

    for peer_row in peers:
        local_peer_id = peer_row['peer_id']
        # Look up peer_shared_id for this local peer_id from this peer's perspective
        peer_self_row = safedb.query_one(
            "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
            (local_peer_id, recorded_by)
        )
        if peer_self_row:
            peer_shared_id = peer_self_row['peer_shared_id']
            # Mark peer as removed in device-wide table by peer_shared_id
            unsafe_db.execute(
                """INSERT OR IGNORE INTO removed_peers (peer_shared_id, removed_at, removed_by)
                   VALUES (?, ?, ?)""",
                (peer_shared_id, removed_at, removed_by)
            )

            # DELETE ALL CONNECTIONS for this peer (enforcement mechanism)
            # This mirrors the enforcement in peer_removed.project()
            cursor = unsafe_db.execute(
                """DELETE FROM sync_connections WHERE peer_shared_id = ?""",
                (peer_shared_id,)
            )
            deleted_count = cursor.rowcount if hasattr(cursor, 'rowcount') else 0
            log.info(f"user_removed.project() deleted {deleted_count} connection(s) for peer {peer_shared_id[:20]}...")

    # Rotate group keys for all groups this user was a member of
    # This prevents the removed user from decrypting future messages
    _rotate_keys_for_removed_user(removed_user_id, recorded_by, removed_at, db)


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
    from events.group import rotate_key
    share_timestamp = t_ms + 1000  # Space out key creation from removal events

    for group_row in group_memberships:
        group_id = group_row['group_id']
        try:
            rotate_key.rotate_group_key(
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
