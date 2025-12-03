"""Network event type - self-signed root of trust for a network."""

# Registry metadata
EVENT_TYPE = 'network'
SHAREABLE = True  # Network root of trust syncs to all peers
EPHEMERAL = False
PROJECTION_TABLE = None

from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a self-signed network event (root of trust).

    The network event is the first shared event in bootstrap. It's self-signed
    using its own keypair (like a root CA certificate). Groups, channels, and
    other content are created AFTER the user has a peer_shared identity.

    Network names are transmitted via encrypted network_name_update events
    to protect privacy from NETWORK ACTIVE ATTACKER.

    Args:
        peer_id: Local peer ID (for recording, not signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        tuple: (network_id, network_private_key)
            - network_id: The stored network event ID (hash of event)
            - network_private_key: Private key for signing bootstrap invites and admin_grant
    """
    log.info(f"network.create() creating self-signed network at t_ms={t_ms}")

    # Generate network's own keypair (network is self-signed)
    network_private_key, network_public_key = crypto.generate_keypair()

    # Create event data - minimal, just the network identity
    # Groups are created LATER after peer_shared exists
    event_data = {
        'type': 'network',
        'network_pubkey': crypto.b64encode(network_public_key),
        'signed_by': 'SELF',  # Special marker for self-signed network event
        'created_at': t_ms
    }

    # Self-sign with network's own private key
    signed_event = crypto.sign_event(event_data, network_private_key)

    # Store as signed plaintext (no encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event and return network_id (which is the event_id)
    network_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"network.create() created self-signed network_id={network_id}")
    return network_id, network_private_key


def project(network_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project self-signed network event into networks table.

    The network event is self-signed (root of trust). Admin grants are handled
    by separate admin_grant events, and groups are created after the network.

    Args:
        network_id: The network event ID
        recorded_by: Peer ID recording this event
        recorded_at: Timestamp when recorded
        db: Database connection

    Returns:
        network_id if successful, None if verification fails
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

    # Verify self-signature using network_pubkey from event body
    signed_by = event_data.get('signed_by')
    network_pubkey_b64 = event_data.get('network_pubkey')

    if signed_by != 'SELF':
        log.warning(f"network.project() expected signed_by='SELF', got {signed_by}")
        return None

    if not network_pubkey_b64:
        log.warning(f"network.project() missing network_pubkey in event")
        return None

    network_pubkey = crypto.b64decode(network_pubkey_b64)
    if not crypto.verify_event(event_data, network_pubkey):
        log.warning(f"network.project() self-signature verification FAILED for network_id={network_id}")
        return None

    log.info(f"network.project() verified self-signed network event")

    # Insert into networks table (minimal - no groups, no creator)
    # Groups and admin grants are separate events created after network
    # Network only stores its own identity data - groups store their network_id/network_role
    safedb.execute(
        """INSERT OR REPLACE INTO networks
           (network_id, creator_user_id, network_pubkey, signed_by, created_at, recorded_by, recorded_at)
           VALUES (?, '', ?, ?, ?, ?, ?)""",
        (
            network_id,
            network_pubkey_b64,
            signed_by,
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )

    log.info(f"network.project() inserted self-signed network in networks table")

    return network_id


def get_all_users_group_id(network_id: str, recorded_by: str, db: Any) -> str:
    """Get all_users group ID for a network.

    The all_users group is cryptographically identified by being signed by the
    network itself (signed_by = network_id). This is the principled way to
    discover which group is the network's main membership group.

    Args:
        network_id: Network ID
        recorded_by: Peer ID querying
        db: Database connection

    Returns:
        All users group ID

    Raises:
        ValueError: If all_users group not found for network
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Query by signature - the all_users group is signed by the network
    group = safedb.query_one(
        "SELECT group_id FROM groups WHERE signed_by = ? AND recorded_by = ?",
        (network_id, recorded_by)
    )

    if not group:
        raise ValueError(f"Network-signed all_users group not found for network {network_id}")

    return group['group_id']


def get_public_key(network_id: str, recorded_by: str, db: Any) -> bytes:
    """Get network public key for signature verification.

    Used to verify signed_by=network_id on bootstrap invites.

    Args:
        network_id: Network ID
        recorded_by: Peer ID querying
        db: Database connection

    Returns:
        Network public key as bytes

    Raises:
        ValueError: If network not found or no network_pubkey
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    network = safedb.query_one(
        "SELECT network_pubkey FROM networks WHERE network_id = ? AND recorded_by = ?",
        (network_id, recorded_by)
    )

    if not network:
        raise ValueError(f"Network {network_id} not found")

    network_pubkey = network['network_pubkey']
    if not network_pubkey:
        raise ValueError(f"Network {network_id} has no network_pubkey")

    return crypto.b64decode(network_pubkey)


def get_for_peer(peer_id: str, recorded_by: str, db: Any) -> dict | None:
    """Get network info for a peer.

    Args:
        peer_id: Peer ID to get network for
        recorded_by: Peer ID querying
        db: Database connection

    Returns:
        Dict with network_id (use get_all_users_group_id/get_admin_group_id for groups)
        or None if peer not in a network
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get user_id for this peer from peers_shared (user→peer relationship stored there)
    peer_row = safedb.query_one(
        "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
        (peer_id, recorded_by)
    )

    if not peer_row or not peer_row['user_id']:
        return None

    # Get network_id from users table using user_id
    user_row = safedb.query_one(
        "SELECT network_id FROM users WHERE user_id = ? AND recorded_by = ?",
        (peer_row['user_id'], recorded_by)
    )

    if not user_row or not user_row['network_id']:
        return None

    network_id = user_row['network_id']

    # Get network details (no longer includes group IDs - query groups table for those)
    network = safedb.query_one(
        "SELECT network_id, network_pubkey, signed_by, created_at FROM networks WHERE network_id = ? AND recorded_by = ?",
        (network_id, recorded_by)
    )

    return network
