"""Transit key event type (device-wide symmetric keys for sync routing)."""
from typing import Any
import json
import logging
import crypto
import store
from db import create_unsafe_db

log = logging.getLogger(__name__)

ID_SIZE = 16  # bytes (128 bits) - BLAKE2b hash size


def create(peer_id: str, t_ms: int, db: Any) -> str:
    """Create a transit key for sync responses, owned by peer_id."""
    log.info(f"transit_key.create() creating new transit key for peer_id={peer_id}, t_ms={t_ms}")

    # Generate symmetric key
    key = crypto.generate_secret()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'transit_key',
        'key': crypto.b64encode(key),
        'created_by': peer_id,  # Local peer who created this key
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store event with recorded wrapper and projection
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"transit_key.create() created key_id={key_id}")
    return key_id


def create_with_material(key_material: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create transit key event with provided key material (for invite transit keys).

    Args:
        key_material: The symmetric key bytes
        peer_id: Peer ID that owns this key
        t_ms: Timestamp
        db: Database connection

    Returns:
        Event ID (to use as hint when wrapping)
    """
    log.info(f"transit_key.create_with_material() creating key for peer_id={peer_id}, t_ms={t_ms}")

    event_data = {
        'type': 'transit_key',
        'key': crypto.b64encode(key_material),
        'created_by': peer_id,  # Local peer who created this key
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"transit_key.create_with_material() created key_id={key_id}")
    return key_id


def project(key_id: str, recorded_by: str, db: Any) -> None:
    """Project transit key event into transit_keys table."""
    log.warning(f"[TRANSIT_KEY_PROJECT] key_id={key_id[:20]}... recorded_by={recorded_by[:10]}...")

    # Get blob from store
    blob = store.get(key_id, db)
    if not blob:
        log.warning(f"[TRANSIT_KEY_PROJECT] result=blob_not_found key_id={key_id[:20]}...")
        return

    # Parse JSON
    event_data = crypto.parse_json(blob)

    # Insert into transit_keys table (device-wide)
    unsafedb = create_unsafe_db(db)
    log.warning(f"[TRANSIT_KEY_PROJECT] result=inserting key_id={key_id[:20]}... owner={event_data['created_by'][:10]}... into_transit_keys_table")
    unsafedb.execute(
        """INSERT OR IGNORE INTO transit_keys (key_id, key, owner_peer_id, created_at)
           VALUES (?, ?, ?, ?)""",
        (
            key_id,
            crypto.b64decode(event_data['key']),
            event_data['created_by'],
            event_data['created_at']
        )
    )

    log.info(f"transit_key.project() projected key_id={key_id} into transit_keys table")


def extract_id(blob: bytes) -> bytes:
    """Extract the first ID_SIZE bytes from a wrapped blob."""
    return blob[:ID_SIZE]


def get_key(key_id: str, recorded_by: str, db: Any) -> dict[str, Any]:
    """Get transit key from database in format expected by crypto.wrap().

    Args:
        key_id: Base64-encoded key ID (event ID)
        recorded_by: Peer ID requesting access (for logging, not enforced for wrapping)
        db: Database connection

    Returns:
        Key dict for crypto.wrap()

    Raises:
        ValueError: If key not found in transit_keys table
    """
    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT key, owner_peer_id FROM transit_keys WHERE key_id = ?", (key_id,))
    if not row:
        raise ValueError(f"transit key not found: {key_id}")

    return {
        'id': crypto.b64decode(key_id),  # Event ID as hint
        'key': row['key'],  # Already bytes from DB
        'type': 'symmetric'
    }


def get_peer_ids_for_key(key_id: str, db: Any) -> list[str]:
    """Get ALL peer_ids that own a specific transit key (for routing).

    Args:
        key_id: Base64-encoded key ID (hint from wrapped blob)
        db: Database connection

    Returns:
        List of peer IDs (may be empty if key not found)
    """
    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one(
        "SELECT owner_peer_id FROM transit_keys WHERE key_id = ?",
        (key_id,)
    )
    if row:
        return [row['owner_peer_id']]

    return []


def get_peer_id_for_key(key_id: str, db: Any) -> str:
    """Get the peer_id that owns a specific transit key.

    Args:
        key_id: Base64-encoded key ID (hint from wrapped blob)
        db: Database connection

    Returns:
        Peer ID string, or empty string if not found
    """
    peer_ids = get_peer_ids_for_key(key_id, db)
    return peer_ids[0] if peer_ids else ""
