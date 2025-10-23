"""Network event type - binds all_users and admins groups, adds creator to admin group."""
from typing import Any
import logging
import crypto
import store
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(all_users_group_id: str, admins_group_id: str, creator_user_id: str,
           peer_id: str, peer_shared_id: str, t_ms: int, db: Any) -> str:
    """Create a network event binding all_users and admins groups.

    The network_id is the event_id (hash of the network event).
    During projection, creator_user_id is automatically added to admin group.

    Args:
        all_users_group_id: Main group for all users
        admins_group_id: Admin-only group
        creator_user_id: User to add to admin group during projection
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        network_id: The stored network event ID (hash of event)
    """
    log.info(f"network.create() creating network with all_users={all_users_group_id}, admins={admins_group_id}, creator={creator_user_id}")

    # Create event data
    event_data = {
        'type': 'network',
        'all_users_group_id': all_users_group_id,
        'admins_group_id': admins_group_id,
        'creator_user_id': creator_user_id,
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext (no encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event and return network_id (which is the event_id)
    network_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"network.create() created network_id={network_id}")
    return network_id


def project(network_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project network event into networks table and add creator to admin group.

    Args:
        network_id: The network event ID
        recorded_by: Peer ID recording this event
        recorded_at: Timestamp when recorded
        db: Database connection

    Returns:
        network_id if successful, None if blocking
    """
    log.debug(f"network.project() projecting network_id={network_id}, recorded_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(network_id, unsafedb)
    if not blob:
        log.warning(f"network.project() blob not found for network_id={network_id}")
        return None

    # Parse JSON (plaintext, no unwrap needed)
    event_data = crypto.parse_json(blob)

    # Verify signature
    from events.identity import peer_shared
    created_by = event_data['created_by']
    try:
        public_key = peer_shared.get_public_key(created_by, recorded_by, db)
    except ValueError:
        log.debug(f"network.project() blocking on peer_shared {created_by}")
        # Don't block here - let recorded.project() handle blocking with recorded_id
        return None

    if not crypto.verify_event(event_data, public_key):
        log.warning(f"network.project() signature verification FAILED for network_id={network_id}")
        return None

    all_users_group_id = event_data['all_users_group_id']
    admins_group_id = event_data['admins_group_id']
    creator_user_id = event_data['creator_user_id']

    # Check if groups exist
    all_users_group = safedb.query_one(
        "SELECT 1 FROM groups WHERE group_id = ? AND recorded_by = ?",
        (all_users_group_id, recorded_by)
    )
    if not all_users_group:
        log.info(f"network.project() blocking on missing all_users group {all_users_group_id}")
        # Don't block here - let recorded.project() handle blocking with recorded_id
        return None

    admins_group = safedb.query_one(
        "SELECT 1 FROM groups WHERE group_id = ? AND recorded_by = ?",
        (admins_group_id, recorded_by)
    )
    if not admins_group:
        log.info(f"network.project() blocking on missing admins group {admins_group_id}")
        # Don't block here - let recorded.project() handle blocking with recorded_id
        return None

    # Check if creator user exists
    creator_user = safedb.query_one(
        "SELECT user_id FROM users WHERE user_id = ? AND recorded_by = ?",
        (creator_user_id, recorded_by)
    )
    if not creator_user:
        log.info(f"network.project() blocking on missing creator user {creator_user_id}")
        # Don't block here - let recorded.project() handle blocking with recorded_id
        return None

    # Insert into networks table
    safedb.execute(
        """INSERT OR IGNORE INTO networks
           (network_id, all_users_group_id, admins_group_id, creator_user_id, created_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            network_id,
            all_users_group_id,
            admins_group_id,
            creator_user_id,
            created_by,
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )

    log.info(f"network.project() inserted into networks table")

    # Add creator to admin group via group_members
    # (Don't create a separate group_member event - just insert directly during projection)
    safedb.execute(
        """INSERT OR IGNORE INTO group_members
           (member_id, group_id, user_id, added_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            f"{network_id}:admin_member",  # Synthetic ID for creator->admin membership
            admins_group_id,
            creator_user_id,
            created_by,  # Added by network creator
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )

    log.info(f"network.project() added creator {creator_user_id} to admin group {admins_group_id}")

    return network_id


def get_admin_group_id(network_id: str, recorded_by: str, db: Any) -> str:
    """Get admin group ID for a network.

    Args:
        network_id: Network ID
        recorded_by: Peer ID querying
        db: Database connection

    Returns:
        Admin group ID

    Raises:
        ValueError: If network not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    network = safedb.query_one(
        "SELECT admins_group_id FROM networks WHERE network_id = ? AND recorded_by = ?",
        (network_id, recorded_by)
    )

    if not network:
        raise ValueError(f"Network {network_id} not found")

    return network['admins_group_id']


def get_all_users_group_id(network_id: str, recorded_by: str, db: Any) -> str:
    """Get all_users group ID for a network.

    Args:
        network_id: Network ID
        recorded_by: Peer ID querying
        db: Database connection

    Returns:
        All users group ID

    Raises:
        ValueError: If network not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    network = safedb.query_one(
        "SELECT all_users_group_id FROM networks WHERE network_id = ? AND recorded_by = ?",
        (network_id, recorded_by)
    )

    if not network:
        raise ValueError(f"Network {network_id} not found")

    return network['all_users_group_id']
