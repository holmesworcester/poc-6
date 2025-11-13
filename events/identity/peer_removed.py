"""Peer removed event type - marks a peer as removed from syncing."""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


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
    """Project peer_removed event to state.

    Mark peer as removed so their sync requests are ignored.
    Historical events from this peer remain valid.

    If this was the last peer of a user, rotate group keys.

    Args:
        event_id: Event ID
        event_data: Event data dictionary
        recorded_by: Peer perspective for key rotation (if this is the last peer)
        db: Database connection
    """
    unsafe_db = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    removed_peer_shared_id = event_data.get('removed_peer_shared_id')
    removed_at = event_data.get('created_at')
    removed_by = event_data.get('removed_by')

    if not removed_peer_shared_id:
        return

    # Insert into removed_peers table (device-wide, no recorded_by)
    unsafe_db.execute(
        """INSERT OR IGNORE INTO removed_peers (peer_shared_id, removed_at, removed_by)
           VALUES (?, ?, ?)""",
        (removed_peer_shared_id, removed_at, removed_by)
    )

    # DELETE ALL CONNECTIONS with this peer (enforcement mechanism)
    # No connections = no sync possible
    # This is the primary enforcement point per the protocol design
    cursor = unsafe_db.execute(
        """DELETE FROM sync_connections WHERE peer_shared_id = ?""",
        (removed_peer_shared_id,)
    )
    deleted_count = cursor.rowcount if hasattr(cursor, 'rowcount') else 0
    log.info(f"peer_removed.project() deleted {deleted_count} connection(s) for removed peer {removed_peer_shared_id[:20]}...")

    # Check if this was the last peer of a user (if so, rotate group keys)
    # Map removed peer to its user_id
    user_row = safedb.query_one(
        "SELECT user_id FROM users WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (removed_peer_shared_id, recorded_by)
    )

    if user_row:
        removed_user_id = user_row['user_id']

        # Check if user has any other peers that are NOT removed
        # Get all peers of this user, then check if any are NOT in removed_peers
        all_user_peers = safedb.query(
            """SELECT u.peer_id FROM users u
               WHERE u.user_id = ? AND u.recorded_by = ?""",
            (removed_user_id, recorded_by)
        )

        # Check how many of these peers are NOT in removed_peers
        unsafe_db = create_unsafe_db(db)
        active_peer_count = 0
        for peer in all_user_peers:
            peer_id = peer['peer_id']
            # Get peer_shared_id for this peer
            peer_self_row = safedb.query_one(
                "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
                (peer_id, recorded_by)
            )
            if peer_self_row:
                peer_shared_id_check = peer_self_row['peer_shared_id']
                # Check if this peer is NOT removed
                removed_check = unsafe_db.query_one(
                    "SELECT 1 FROM removed_peers WHERE peer_shared_id = ? LIMIT 1",
                    (peer_shared_id_check,)
                )
                if not removed_check:
                    active_peer_count += 1

        other_peers = [{'count': active_peer_count}]

        # If no other peers remain (this was the last one), rotate keys
        if other_peers and other_peers[0]['count'] == 0:
            log.info(f"peer_removed.project() removed_peer {removed_peer_shared_id[:20]}... was the last peer of user {removed_user_id[:20]}..., rotating group keys")
            _rotate_keys_for_removed_peer_user(removed_user_id, recorded_by, removed_at, db)
        else:
            log.info(f"peer_removed.project() removed_peer {removed_peer_shared_id[:20]}... had other active peers, no key rotation needed")


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
            log.info(f"peer_removed._rotate_keys_for_removed_peer_user() rotated key for group {group_id[:20]}...")
        except Exception as e:
            log.warning(f"peer_removed._rotate_keys_for_removed_peer_user() failed to rotate key for group {group_id[:20]}...: {e}")
            # Continue rotating keys for other groups even if one fails
