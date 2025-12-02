"""Network event type - self-signed root of trust for a network.

SPEC - declarative metadata for generic resolver
project() - pure function: input_dict -> ProjectorResult

API functions:
    create(peer_id, t_ms, db) -> tuple[str, bytes]
    project_event(network_id, recorded_by, recorded_at, db) -> str | None
    get_all_users_group_id(network_id, recorded_by, db) -> str
    get_public_key(network_id, recorded_by, db) -> bytes
    get_for_peer(peer_id, recorded_by, db) -> dict | None
"""
from typing import Any, TypedDict
import logging
import crypto
import store
from db import create_safe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class NetworkEventData(TypedDict):
    type: str
    signed_by: str  # Always 'SELF' for networks
    network_pubkey: str  # Base64-encoded public key
    created_at: int


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,
    "signer_type": "self",  # Self-signed (root of trust)
    "dependencies": [],
    "tables": ["networks"],
    "generic_dispatch": True,
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result. No database access."""
    from projection import ProjectorResult

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    # Validate signed_by is 'SELF'
    signed_by = event_data.get("signed_by")
    if signed_by != "SELF":
        return ProjectorResult(valid=False, reason=f"Expected signed_by='SELF', got {signed_by}")

    # Check signature
    if not input_dict.get("signature_valid"):
        return ProjectorResult(valid=False, reason="Self-signature verification failed")

    # Check required field
    network_pubkey = event_data.get("network_pubkey")
    if not network_pubkey:
        return ProjectorResult(valid=False, reason="Missing network_pubkey in event")

    # Build output row
    network_row = {
        "network_id": event_id,
        "creator_user_id": "",  # Set later by admin.project_event()
        "network_pubkey": network_pubkey,
        "signed_by": signed_by,
        "created_at": event_data["created_at"],
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(valid=True, tables={"networks": [network_row]})


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    network_pubkey: str = "pubkey_abc123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "network",
        "signed_by": "SELF",
        "network_pubkey": network_pubkey,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "net_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    signature_valid: bool = True,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "signature_valid": signature_valid,
        "dependencies": {},
    }


# ============================================================================
# API FUNCTIONS
# ============================================================================

def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a self-signed network event (root of trust).

    The network event is the first shared event in bootstrap. It's self-signed
    using its own keypair (like a root CA certificate). Groups, channels, and
    other content are created AFTER the user has a peer_shared identity.

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


def project_event(network_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project self-signed network event into networks table.

    Uses pure functional projector.
    """
    log.debug(f"network.project_event() projecting network_id={network_id[:20]}...")

    from projection import resolve, apply_result

    input_dict = resolve("network", network_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = project(input_dict)

    if result.blocked or not result.valid:
        log.warning(f"network.project_event() failed: {result.reason}")
        return None

    apply_result(result, recorded_by, recorded_at, db)

    log.info(f"network.project_event() projected network_id={network_id[:20]}...")
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

    Phase 4: Used to verify signed_by=network_id on bootstrap invites.

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
        raise ValueError(f"Network {network_id} has no network_pubkey (legacy event)")

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
