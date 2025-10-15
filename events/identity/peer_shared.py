"""Peer shared event type (shareable public identity)."""
from typing import Any
import json
import logging
import crypto
import store
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, t_ms: int, db: Any) -> str:
    """Create a shareable peer_shared event from a local peer."""
    log.info(f"peer_shared.create() creating peer_shared for peer_id={peer_id}, t_ms={t_ms}")

    # Get keys from local peer
    public_key = peer.get_public_key(peer_id, peer_id, db)
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Create event dict (plaintext, will be signed)
    event_data = {
        'type': 'peer_shared',
        'public_key': crypto.b64encode(public_key),
        'peer_id': peer_id,  # Link back to local peer
        'created_at': t_ms
    }

    # Sign the event with peer's private key
    signed_event = crypto.sign_event(event_data, private_key)

    # Canonicalize to get deterministic blob
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    peer_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"peer_shared.create() created peer_shared_id={peer_shared_id}")
    return peer_shared_id


def project(peer_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project peer_shared event into peers_shared and shareable_events tables."""
    log.warning(f"[PEER_SHARED_PROJECT] peer_shared_id={peer_shared_id[:20]}..., recorded_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(peer_shared_id, unsafedb)
    if not blob:
        log.warning(f"peer_shared.project() blob not found for peer_shared_id={peer_shared_id}")
        return None

    # Parse JSON (signed plaintext)
    event_data = crypto.parse_json(blob)

    # Verify signature using public key from event itself
    public_key_b64 = event_data.get('public_key')
    if not public_key_b64:
        log.warning(f"peer_shared.project() missing public_key in event data")
        return None

    public_key = crypto.b64decode(public_key_b64)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"peer_shared.project() signature verification failed for peer_shared_id={peer_shared_id}")
        return None

    # Insert into peers_shared table
    safedb.execute(
        """INSERT OR IGNORE INTO peers_shared
           (peer_shared_id, peer_id, public_key, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            peer_shared_id,
            event_data['peer_id'],
            event_data['public_key'],
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )
    log.info(f"peer_shared.project() inserted into peers_shared: peer_shared_id={peer_shared_id}, owner_peer_id={event_data['peer_id']}, recorded_by={recorded_by}")

    # Insert into peer_self table (subjective mapping) if this is our own peer
    owner_peer_id = event_data['peer_id']
    if owner_peer_id == recorded_by:
        safedb.execute(
            "INSERT OR REPLACE INTO peer_self (peer_id, peer_shared_id, recorded_by, recorded_at) VALUES (?, ?, ?, ?)",
            (owner_peer_id, peer_shared_id, recorded_by, recorded_at)
        )
        log.info(f"peer_shared.project() inserted into peer_self: peer_id={owner_peer_id[:20]}..., peer_shared_id={peer_shared_id[:20]}..., recorded_by={recorded_by[:20]}...")

    # Mark as valid for this peer
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (peer_shared_id, recorded_by)
    )

    return peer_shared_id


def get_public_key(peer_shared_id: str, recorded_by: str, db: Any) -> bytes:
    """Get public key for a peer_shared_id from the perspective of recorded_by."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT public_key FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, recorded_by)
    )
    if not row:
        raise ValueError(f"peer_shared not found: {peer_shared_id} for peer {recorded_by}")
    # public_key is stored as base64 string
    return crypto.b64decode(row['public_key'])


def get_peer_id_for_signing(peer_shared_id: str, recorded_by: str, db: Any) -> str:
    """Get the local peer_id associated with a peer_shared_id for signing.

    Args:
        peer_shared_id: The public peer_shared ID
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Local peer_id for signing

    Raises:
        ValueError: If peer_shared not found or peer doesn't have access
    """
    unsafedb = create_unsafe_db(db)

    # Get the event blob
    blob = store.get(peer_shared_id, unsafedb)
    if not blob:
        raise ValueError(f"peer_shared not found: {peer_shared_id}")

    event_data = crypto.parse_json(blob)
    peer_id = event_data.get('peer_id')
    if not peer_id:
        raise ValueError(f"peer_id not found in peer_shared event: {peer_shared_id}")

    # Security: Only allow access if the requester owns this peer_shared_id
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access signing info for peer_shared {peer_shared_id}")

    return peer_id
