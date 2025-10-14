"""Peer event type (local-only identity keypair)."""
from typing import Any
import json
import logging
import crypto
import store
from db import create_unsafe_db

log = logging.getLogger(__name__)


def create(t_ms: int, db: Any) -> tuple[str, str]:
    """Create a peer (local-only keypair) and its shareable peer_shared event.

    Returns (peer_id, peer_shared_id): peer_id is local (for signing), peer_shared_id is public (for created_by).
    """
    from events.identity import peer_shared

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
    from events.transit import recorded
    recorded_id = recorded.create(peer_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    # Create shareable peer_shared event
    peer_shared_id = peer_shared.create(peer_id, t_ms, db)
    log.info(f"peer.create() created peer_shared_id={peer_shared_id}")

    return (peer_id, peer_shared_id)


def project(peer_id: str, recorded_by: str, db: Any) -> None:
    """Project peer event into peers table (for local peers, both IDs are the same)."""
    log.debug(f"peer.project() projecting peer_id={peer_id}, seen_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(peer_id, unsafedb)
    if not blob:
        log.warning(f"peer.project() blob not found for peer_id={peer_id}")
        return

    # Parse JSON
    event_data = crypto.parse_json(blob)

    # Insert into local_peers table (local-only, not shareable)
    # Note: peer_id → peer_shared_id mapping is stored in peer_self table (subjective)
    unsafedb.execute(
        """INSERT OR IGNORE INTO local_peers (peer_id, public_key, private_key, created_at)
           VALUES (?, ?, ?, ?)""",
        (
            peer_id,
            event_data['public_key'],
            crypto.b64decode(event_data['private_key']),
            event_data['created_at']
        )
    )

    # Mark as valid for this peer
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
