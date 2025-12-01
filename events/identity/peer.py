"""Peer event type (local-only identity keypair)."""
from typing import Any

# Registry metadata
EVENT_TYPE = 'peer'
SHAREABLE = False  # Local-only - contains private key material
EPHEMERAL = False
PROJECTION_TABLE = None  # No projection table (stored in peers table)
import json
import logging
import crypto
import store
from db import create_unsafe_db

log = logging.getLogger(__name__)


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
    from projectors import resolve, apply_result_device_wide
    from projectors import peer as peer_projector
    from db import create_safe_db

    log.debug(f"peer.project() projecting peer_id={peer_id}, seen_by={recorded_by}")

    # Use recorded_at=0 since peer events use created_at from blob
    input_dict = resolve("peer", peer_id, recorded_by, 0, db)
    if not input_dict:
        log.warning(f"peer.project() resolve failed for peer_id={peer_id}")
        return

    result = peer_projector.project(input_dict)

    if not result.valid:
        log.warning(f"peer.project() rejected: {result.reason}")
        return

    # Apply to device-wide table
    apply_result_device_wide(result, input_dict["recorded_at"], db)

    # Mark as valid for this peer
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (peer_id, recorded_by)
    )

    log.info(f"peer.project() projected peer_id={peer_id} into peers table")


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
