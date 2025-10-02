"""Key event type (local-only symmetric encryption key)."""
from typing import Any
import json
import logging
import crypto
import store

log = logging.getLogger(__name__)

ID_SIZE = 16  # bytes (128 bits) - BLAKE2b hash size


def create(peer_id: str, t_ms: int, db: Any) -> str:
    """Create a local-only symmetric encryption key, owned by peer_id."""
    log.info(f"key.create() creating new key for peer_id={peer_id}, t_ms={t_ms}")

    # Generate symmetric key
    key = crypto.generate_secret()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'key',
        'key': crypto.b64encode(key),
        'peer_id': peer_id,  # Store which peer owns this key
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store event with first_seen wrapper and projection
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"key.create() created key_id={key_id}")
    return key_id


def project(key_id: str, seen_by_peer_id: str, db: Any) -> None:
    """Project key event into keys table and mark valid for owning peer."""
    log.debug(f"key.project() projecting key_id={key_id}, seen_by={seen_by_peer_id}")

    # Get blob from store
    blob = store.get(key_id, db)
    if not blob:
        log.warning(f"key.project() blob not found for key_id={key_id}")
        return

    # Parse JSON
    event_data = crypto.parse_json(blob)

    # Insert into keys table (local-only, not shareable)
    db.execute(
        """INSERT OR IGNORE INTO keys (key_id, key, created_at)
           VALUES (?, ?, ?)""",
        (
            key_id,
            crypto.b64decode(event_data['key']),
            event_data['created_at']
        )
    )

    # Mark as valid for this peer
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, seen_by_peer_id) VALUES (?, ?)",
        (key_id, seen_by_peer_id)
    )

    log.info(f"key.project() projected key_id={key_id} into keys table")

    # Keys are local-only and should NOT be added to shareable_events
    # Key sharing happens explicitly via key_shared events


def extract_id(blob: bytes) -> bytes:
    """Extract the first ID_SIZE bytes from a wrapped blob."""
    return blob[:ID_SIZE]


def get_key_by_id(id_bytes: bytes, db: Any) -> dict[str, Any] | None:
    """Get key from database by id bytes. Checks both symmetric keys and asymmetric prekeys.

    Args:
        id_bytes: Key ID bytes (16 bytes)
        db: Database connection

    Returns:
        Key dict for crypto.unwrap(), or None if not found
    """
    key_id = crypto.b64encode(id_bytes)

    # First try symmetric keys table
    row = db.query_one("SELECT key FROM keys WHERE key_id = ?", (key_id,))
    if row:
        return {
            'id': id_bytes,
            'key': row['key'],
            'type': 'symmetric'
        }

    # Then try asymmetric prekeys table (id_bytes = peer_id for prekeys)
    # Note: For prekeys to work with unwrap, the blob must be prefixed with the peer_id
    prekey_row = db.query_one("SELECT private_key FROM peers WHERE peer_id = ?", (key_id,))
    if prekey_row:
        return {
            'id': id_bytes,
            'private_key': prekey_row['private_key'],
            'type': 'asymmetric'
        }

    return None


def get_key(key_id: str, db: Any) -> dict[str, Any]:
    """Get key from database in format expected by crypto.wrap()."""
    row = db.query_one("SELECT key FROM keys WHERE key_id = ?", (key_id,))
    if not row:
        raise ValueError(f"key not found: {key_id}")

    return {
        'id': crypto.b64decode(key_id),  # Decode base64 key_id to bytes for use as blob prefix
        'key': row['key'],  # Already bytes from DB
        'type': 'symmetric'
    }


def get_peer_id_for_key(key_id: str, db: Any) -> str:
    """Get the peer_id that owns a specific key.

    Args:
        key_id: Base64-encoded key ID (hint from wrapped blob)
        db: Database connection

    Returns:
        Peer ID string, or empty string if not found
    """
    # First check if this is a symmetric key event in the store
    key_blob = store.get(key_id, db)
    if key_blob:
        try:
            event_data = crypto.parse_json(key_blob)
            peer_id = event_data.get('peer_id')
            if peer_id:
                return peer_id
        except:
            pass

    # If not found in store, check if this key_id IS a peer_id (for asymmetric prekeys)
    # Prekey-wrapped blobs use peer_id as the hint
    peer_row = db.query_one("SELECT peer_id FROM peers WHERE peer_id = ?", (key_id,))
    if peer_row:
        return peer_row['peer_id']

    return ""
