"""Peer event type (local-only identity keypair).

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    create(t_ms, db) -> str
    project_event(peer_id, recorded_by, db) -> None
    get_private_key(peer_id, recorded_by, db) -> bytes
    get_public_key(peer_id, recorded_by, db) -> bytes
"""
from typing import Any, TypedDict
import json
import logging
import crypto
import store
from db import create_unsafe_db

log = logging.getLogger(__name__)

# Registry metadata
EVENT_TYPE = 'peer'
SHAREABLE = False  # Local-only - contains private key material
EPHEMERAL = False
PROJECTION_TABLE = None  # No projection table (stored in peers table)


# ============================================================================
# TYPES
# ============================================================================

class PeerEventData(TypedDict):
    type: str
    public_key: str  # base64
    private_key: str  # base64
    created_at: int


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result.

    Outputs local_peers row. Use apply_result_device_wide() to write.
    """
    from projection import ProjectorResult

    event_id = input_dict["event_id"]  # peer_id
    event_data = input_dict["event_data"]

    public_key_b64 = event_data.get("public_key")
    private_key_b64 = event_data.get("private_key")
    created_at = event_data.get("created_at")

    if not all([public_key_b64, private_key_b64, created_at is not None]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Decode private key (stored as bytes in DB)
    private_key = crypto.b64decode(private_key_b64)

    # Output: local_peers row (device-wide table)
    # Note: public_key stays as base64 string, private_key is bytes
    row = {
        "peer_id": event_id,
        "public_key": public_key_b64,
        "private_key": private_key,
        "created_at": created_at,
    }

    return ProjectorResult(
        valid=True,
        tables={"local_peers": [row]},
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    public_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 bytes base64
    private_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 bytes base64
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "peer",
        "public_key": public_key,
        "private_key": private_key,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "peer_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_123",  # Usually same as event_id for local peers
    recorded_at: int = 1000001,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }


# ============================================================================
# API FUNCTIONS
# ============================================================================


def create(t_ms: int, db: Any) -> str:
    """Create a peer (local-only keypair).

    Returns peer_id: the local peer ID for signing operations.

    Note: peer_shared is NOT created here. It is created during network join
    (via invite) so that every peer_shared is invite-signed and linked to a user_id.
    """
    log.info(f"peer.create() creating new peer at t_ms={t_ms}")

    unsafedb = create_unsafe_db(db)

    # Generate keypair
    private_key, public_key = crypto.generate_keypair()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'peer',
        'public_key': crypto.b64encode(public_key),
        'private_key': crypto.b64encode(private_key),
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # First store the blob to get the peer_id
    peer_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    log.info(f"peer.create() generated peer_id={peer_id}")

    # Then create recorded wrapper where peer sees itself
    from events.network import recorded
    recorded_id = recorded.create(peer_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project_event(recorded_id, db)

    return peer_id


def project_event(peer_id: str, recorded_by: str, db: Any) -> None:
    """Project peer event using pure projector.

    Uses apply_result_device_wide since local_peers is device-wide.
    """
    from projection import resolve, apply_result_device_wide
    from db import create_safe_db

    log.debug(f"peer.project_event() projecting peer_id={peer_id}, seen_by={recorded_by}")

    # Use recorded_at=0 since peer events use created_at from blob
    input_dict = resolve("peer", peer_id, recorded_by, 0, db)
    if not input_dict:
        log.warning(f"peer.project_event() resolve failed for peer_id={peer_id}")
        return

    result = project(input_dict)

    if not result.valid:
        log.warning(f"peer.project_event() rejected: {result.reason}")
        return

    # Apply to device-wide table
    apply_result_device_wide(result, input_dict["recorded_at"], db)

    # Mark as valid for this peer
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (peer_id, recorded_by)
    )

    log.info(f"peer.project_event() projected peer_id={peer_id} into peers table")


def get_private_key(peer_id: str, recorded_by: str, db: Any) -> bytes:
    """Get private key for a peer_id.

    Args:
        peer_id: Peer ID to get private key for
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Private key bytes

    Raises:
        ValueError: If peer not found or recorded_by != peer_id (can only access your own private key)
    """
    # Security: Only allow a peer to access their own private key
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access private key for peer {peer_id}")

    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT private_key FROM local_peers WHERE peer_id = ?", (peer_id,))
    if not row:
        raise ValueError(f"peer not found: {peer_id}")
    return row['private_key']


def get_public_key(peer_id: str, recorded_by: str, db: Any) -> bytes:
    """Get public key for a peer_id from local_peers table.

    Args:
        peer_id: Peer ID to get public key for
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Public key bytes

    Raises:
        ValueError: If peer not found or recorded_by != peer_id (can only access your own peer's public key from local_peers)
    """
    # Security: Only allow a peer to access their own local peer's public key
    # (Public keys for other peers should come from peer_shared events, not local_peers)
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access local peer public key for peer {peer_id}")

    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT public_key FROM local_peers WHERE peer_id = ?", (peer_id,))
    if not row:
        raise ValueError(f"peer not found: {peer_id}")
    # public_key is stored as base64 string in the table
    return crypto.b64decode(row['public_key'])
