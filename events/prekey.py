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
    Projects prekey_private to peers table.

    Args:
        peer_id: Local peer ID (for projection and storage)
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

    # Create first_seen wrapper where peer sees itself
    from events import first_seen
    first_seen_id = first_seen.create(prekey_id, peer_id, t_ms, db, return_dupes=False)
    first_seen.project(first_seen_id, db)

    log.info(f"prekey.create() projected prekey_id={prekey_id}")
    return prekey_id, prekey_private


def project(prekey_id: str, seen_by_peer_id: str, db: Any) -> None:
    """Project prekey event into peers table (sets prekey_private column)."""
    log.info(f"prekey.project() prekey_id={prekey_id}, seen_by={seen_by_peer_id}")

    # Get blob from store
    blob = store.get(prekey_id, db)
    if not blob:
        log.warning(f"prekey.project() blob not found for prekey_id={prekey_id}")
        return

    # Parse JSON
    event_data = crypto.parse_json(blob)

    # Update peers table with prekey_private
    db.execute(
        "UPDATE peers SET prekey_private = ? WHERE peer_id = ?",
        (crypto.b64decode(event_data['private_key']), event_data['peer_id'])
    )

    # Insert into pre_keys table with public key (for others to encrypt sync requests)
    db.execute(
        "INSERT OR REPLACE INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
        (event_data['peer_id'], crypto.b64decode(event_data['public_key']), event_data['created_at'])
    )

    # Mark as valid for this peer
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, seen_by_peer_id) VALUES (?, ?)",
        (prekey_id, seen_by_peer_id)
    )


def get_transit_prekey_for_peer(peer_id: str, db: Any) -> dict[str, Any] | None:
    """Get the transit pre-key for a specific peer in format expected by crypto.wrap().

    Args:
        peer_id: Peer ID to get prekey for
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
