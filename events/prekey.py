"""Prekey event type (local-only prekey keypair)."""
from typing import Any
import json
import logging
import crypto
import store

log = logging.getLogger(__name__)


def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a local-only prekey event (like peer.create).

    Generates Ed25519 keypair, stores both public and private keys in event.
    Projects to local_prekeys table with owner_peer_id.

    Args:
        peer_id: Local peer ID (owner of this prekey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (prekey_id, prekey_private): The stored prekey event ID and private key bytes
    """
    log.info(f"prekey.create() creating new prekey for peer_id={peer_id}, t_ms={t_ms}")

    # Generate Ed25519 keypair for prekey
    prekey_private, prekey_public = crypto.generate_keypair()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'prekey',
        'public_key': crypto.b64encode(prekey_public),
        'private_key': crypto.b64encode(prekey_private),
        'peer_id': peer_id,
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store the blob to get prekey_id
    prekey_id = store.blob(blob, t_ms, return_dupes=True, db=db)
    log.info(f"prekey.create() generated prekey_id={prekey_id}")

    # Create recorded wrapper where peer sees itself
    from events import recorded
    recorded_id = recorded.create(prekey_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    log.info(f"prekey.create() projected prekey_id={prekey_id}")
    return prekey_id, prekey_private


def project(prekey_id: str, recorded_by: str, db: Any) -> None:
    """Project prekey event into local_prekeys table with owner_peer_id."""
    log.info(f"prekey.project() prekey_id={prekey_id}, seen_by={recorded_by}")

    # Get blob from store
    blob = store.get(prekey_id, db)
    if not blob:
        log.warning(f"prekey.project() blob not found for prekey_id={prekey_id}")
        return

    # Parse JSON
    event_data = crypto.parse_json(blob)
    owner_peer_id = event_data['peer_id']

    # Delete old prekeys for this owner (keep only one active prekey per peer)
    db.execute("DELETE FROM local_prekeys WHERE owner_peer_id = ?", (owner_peer_id,))

    # Insert into local_prekeys table with owner
    db.execute(
        "INSERT INTO local_prekeys (prekey_id, owner_peer_id, public_key, private_key, created_at) VALUES (?, ?, ?, ?, ?)",
        (prekey_id, owner_peer_id, crypto.b64decode(event_data['public_key']),
         crypto.b64decode(event_data['private_key']), event_data['created_at'])
    )

    # Insert into pre_keys table with public key (for others to encrypt sync requests)
    # Use owner_peer_id as the identifier (pre_keys maps peer_id -> public_key)
    db.execute(
        "INSERT OR REPLACE INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
        (owner_peer_id, crypto.b64decode(event_data['public_key']), event_data['created_at'])
    )

    # Mark as valid for this peer
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (prekey_id, recorded_by)
    )


def get_transit_prekey_for_peer(peer_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the transit pre-key for a specific peer in format expected by crypto.wrap().

    Prekeys are public and meant to be shared for encryption, but we add recorded_by
    for consistency with other access patterns. No strict access control is enforced since
    prekeys are intentionally public for anyone to encrypt messages.

    Args:
        peer_id: Peer ID to get prekey for
        recorded_by: Peer ID requesting access (for logging/consistency, not enforced)
        db: Database connection

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'type': 'asymmetric'}
        or None if prekey not found
    """
    result = db.query_one("SELECT public_key FROM pre_keys WHERE peer_id = ?", (peer_id,))
    if not result:
        return None

    # Use peer_id as the hint/id for asymmetric keys
    peer_id_bytes = crypto.b64decode(peer_id)

    return {
        'id': peer_id_bytes,
        'public_key': result['public_key'],
        'type': 'asymmetric'
    }
