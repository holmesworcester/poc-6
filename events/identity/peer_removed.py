"""Peer removed event type - marks a peer as removed from syncing."""

# Registry metadata
EVENT_TYPE = 'peer_removed'
SHAREABLE = True  # Peer removal syncs to stop syncing with removed peer
EPHEMERAL = False
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from core.db import create_safe_db, create_unsafe_db

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
    # store.event() creates recorded wrapper and triggers projection
    blob = crypto.canonicalize_json(signed_event)
    event_id = store.event(blob, removed_by_local_peer_id, t_ms, db)

    return event_id


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project peer_removed event to state.

    Mark peer as removed so their sync requests are ignored.
    Historical events from this peer remain valid.

    If this was the last peer of a user, rotate group keys.

    Args:
        event_id: Event ID
        recorded_by: Peer perspective for key rotation (if this is the last peer)
        recorded_at: When the event was recorded
        db: Database connection

    Returns:
        event_id if successful, None otherwise
    """
    # Fetch and parse event data from store
    blob = store.get(event_id, db)
    if not blob:
        log.warning(f"peer_removed.project() blob not found for {event_id[:20]}...")
        return None

    event_data = crypto.parse_json(blob)

    # Verify signature before trusting event data
    # peer_removed uses 'removed_by' as the signer field (not 'signed_by')
    signer_peer_shared_id = event_data.get('removed_by')
    if not signer_peer_shared_id:
        log.warning(f"peer_removed.project() missing removed_by field for {event_id[:20]}...")
        return None

    from events.identity import peer_shared
    try:
        public_key = peer_shared.get_public_key(signer_peer_shared_id, recorded_by, db)
        if not crypto.verify_event(event_data, public_key):
            log.warning(f"peer_removed.project() signature verification failed for {event_id[:20]}...")
            return None
    except ValueError:
        log.warning(f"peer_removed.project() signer peer_shared not found for {event_id[:20]}...")
        return None

    unsafe_db = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    removed_peer_shared_id = event_data.get('removed_peer_shared_id')
    removed_at = event_data.get('created_at')
    signed_by = event_data.get('removed_by')  # The remover's peer_shared_id is the signer

    if not removed_peer_shared_id:
        return None

    # Insert into removed_peers table (device-wide, no recorded_by)
    unsafe_db.execute(
        """INSERT OR IGNORE INTO removed_peers (peer_shared_id, removed_at, signed_by)
           VALUES (?, ?, ?)""",
        (removed_peer_shared_id, removed_at, signed_by)
    )

    # DELETE ALL CONNECTIONS with this peer (enforcement mechanism)
    # No connections = no sync possible
    # Uses connection module API which handles per-peer connection removal
    from events.network import connection
    deleted_count = connection.remove_connections_for_peer(removed_peer_shared_id, db)
    log.info(f"peer_removed.project() deleted {deleted_count} connection(s) for removed peer {removed_peer_shared_id[:20]}...")

    # Rotate group keys when ANY peer is removed
    # This ensures removed peers cannot decrypt future messages, even if they have cached keys
    # The new key is shared with remaining members via group_key_shared events
    # Map removed peer to its user_id from peers_shared (user→peer relationship stored there)
    peer_row = safedb.query_one(
        "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (removed_peer_shared_id, recorded_by)
    )

    if peer_row and peer_row['user_id']:
        removed_user_id = peer_row['user_id']
        log.info(f"peer_removed.project() removed_peer {removed_peer_shared_id[:20]}... belongs to user {removed_user_id[:20]}..., rotating group keys")
        _rotate_keys_for_removed_peer_user(removed_user_id, recorded_by, removed_at, db)

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
