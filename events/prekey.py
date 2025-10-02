"""Pre-key management functions."""
from typing import Any
import crypto
import store
from events import key, peer


def create(peer_id: str, peer_shared_id: str, group_id: str,
           key_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a prekey event for receiving sync requests.

    Generates Ed25519 keypair, stores private key in peers table,
    publishes public key as shareable event.

    Args:
        peer_id: Local peer ID (for signing and storing private key)
        peer_shared_id: Public peer ID (for created_by)
        group_id: Group context (for access control)
        key_id: Key to encrypt the prekey event with
        t_ms: Timestamp
        db: Database connection

    Returns:
        (prekey_id, prekey_private): The stored prekey event ID and private key bytes
    """
    # Generate Ed25519 keypair for prekey
    prekey_private, prekey_public = crypto.generate_keypair()
    prekey_public_b64 = crypto.b64encode(prekey_public)

    # Store private key in peers table (need to add prekey_private column)
    db.execute(
        "UPDATE peers SET prekey_private = ? WHERE peer_id = ?",
        (prekey_private, peer_id)
    )

    # Create prekey event
    event_data = {
        'type': 'prekey',
        'peer_id': peer_shared_id,
        'group_id': group_id,
        'public_key': prekey_public_b64,
        'created_by': peer_shared_id,
        'created_at': t_ms,
        'key_id': key_id
    }

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get key_data for encryption
    key_data = key.get_key(key_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event with first_seen wrapper and projection
    prekey_id = store.event(blob, peer_id, t_ms, db)

    return prekey_id, prekey_private


def project(prekey_id: str, seen_by_peer_id: str, received_at: int, db: Any) -> str | None:
    """Project prekey event into pre_keys table."""
    # Get blob from store
    blob = store.get(prekey_id, db)
    if not blob:
        return None

    # Unwrap (decrypt)
    unwrapped, _ = crypto.unwrap(blob, db)
    if not unwrapped:
        return None

    # Parse JSON
    event_data = crypto.parse_json(unwrapped)

    # Verify signature - get public key from created_by peer_shared
    from events import peer_shared
    created_by = event_data['created_by']
    public_key = peer_shared.get_public_key(created_by, seen_by_peer_id, db)
    if not crypto.verify_event(event_data, public_key):
        return None

    # Insert into pre_keys table
    prekey_public = crypto.b64decode(event_data['public_key'])
    db.execute(
        """INSERT OR IGNORE INTO pre_keys
           (peer_id, public_key, created_at)
           VALUES (?, ?, ?)""",
        (
            event_data['peer_id'],
            prekey_public,
            event_data['created_at']
        )
    )

    # Insert into shareable_events
    db.execute(
        """INSERT OR IGNORE INTO shareable_events (event_id, peer_id, created_at)
           VALUES (?, ?, ?)""",
        (
            prekey_id,
            event_data['created_by'],
            event_data['created_at']
        )
    )

    # Mark as valid for this peer
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, seen_by_peer_id) VALUES (?, ?)",
        (prekey_id, seen_by_peer_id)
    )

    return prekey_id


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
