"""Key event type (local-only symmetric encryption key)."""
from typing import Any
import json
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

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

    # Store event with recorded wrapper and projection
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"key.create() created key_id={key_id}")
    return key_id


def create_with_material(key_material: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create key event with provided key material (for invite keys).

    Args:
        key_material: The symmetric key bytes
        peer_id: Peer ID that owns this key
        t_ms: Timestamp
        db: Database connection

    Returns:
        Event ID (to use as hint when wrapping)
    """
    log.info(f"key.create_with_material() creating key for peer_id={peer_id}, t_ms={t_ms}")

    event_data = {
        'type': 'key',
        'key': crypto.b64encode(key_material),
        'peer_id': peer_id,
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"key.create_with_material() created key_id={key_id}")
    return key_id


def project(key_id: str, recorded_by: str, db: Any) -> None:
    """Project key event into keys table and mark valid for owning peer."""
    log.debug(f"key.project() projecting key_id={key_id}, seen_by={recorded_by}")

    # Get blob from store
    blob = store.get(key_id, db)
    if not blob:
        log.warning(f"key.project() blob not found for key_id={key_id}")
        return

    # Parse JSON
    event_data = crypto.parse_json(blob)

    # Insert into keys table (local-only, not shareable)
    # Store event ID as key_id for lookups
    unsafedb = create_unsafe_db(db)
    log.debug(f"key.project() inserting key_id={key_id} into keys table")
    unsafedb.execute(
        """INSERT OR IGNORE INTO keys (key_id, key, created_at)
           VALUES (?, ?, ?)""",
        (
            key_id,
            crypto.b64decode(event_data['key']),
            event_data['created_at']
        )
    )

    # Track key ownership for routing
    unsafedb.execute(
        "INSERT OR IGNORE INTO key_ownership (key_id, peer_id, created_at) VALUES (?, ?, ?)",
        (key_id, recorded_by, event_data['created_at'])
    )

    # Mark as valid for this peer
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (key_id, recorded_by)
    )

    log.info(f"key.project() projected key_id={key_id} into keys table")

    # Keys are local-only and should NOT be added to shareable_events
    # Key sharing happens explicitly via key_shared events


def extract_id(blob: bytes) -> bytes:
    """Extract the first ID_SIZE bytes from a wrapped blob."""
    return blob[:ID_SIZE]


def get_key_by_id(id_bytes: bytes, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get key from database by id bytes. Checks both symmetric keys and asymmetric prekeys.

    Args:
        id_bytes: Key ID bytes (16 bytes) - hint ID (hash of key material)
        recorded_by: Peer ID attempting to access this key (for ownership filtering)
        db: Database connection

    Returns:
        Key dict for crypto.unwrap(), or None if not found or not owned by recorded_by
    """
    key_id = crypto.b64encode(id_bytes)

    log.debug(f"get_key_by_id() looking up key_id={key_id}, recorded_by={recorded_by[:20]}...")

    # First try symmetric keys table (no ownership check - keys are global)
    # Direct lookup by event ID (hint is event ID, not hash of key material)
    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT key FROM keys WHERE key_id = ?", (key_id,))
    if row:
        log.debug(f"get_key_by_id() found symmetric key for key_id={key_id}")
        return {
            'id': id_bytes,
            'key': row['key'],
            'type': 'symmetric'
        }

    log.debug(f"get_key_by_id() key_id={key_id} not found in keys table")

    # Then try asymmetric keys from prekeys (with ownership filter)
    # The hint (key_id) is now always a prekey_shared_id (event ID)
    # Look up the private key by prekey_shared_id
    log.debug(f"get_key_by_id() checking prekeys table for prekey_shared_id={key_id}, owner={recorded_by[:20]}...")
    prekey_row = unsafedb.query_one(
        "SELECT private_key FROM prekeys WHERE prekey_shared_id = ? AND owner_peer_id = ? LIMIT 1",
        (key_id, recorded_by)
    )
    if prekey_row and prekey_row['private_key']:
        log.debug(f"get_key_by_id() found prekey private key for prekey_shared_id={key_id}")
        return {
            'id': id_bytes,
            'private_key': prekey_row['private_key'],
            'type': 'asymmetric'
        }

    log.debug(f"get_key_by_id() prekey not found for prekey_shared_id={key_id}")

    # Finally, try main peer private key from local_peers (with ownership filter)
    # The hint (key_id) is the peer_id, so check if it matches recorded_by
    log.debug(f"get_key_by_id() checking local_peers for peer_id={key_id}")
    peer_row = unsafedb.query_one(
        "SELECT private_key FROM local_peers WHERE peer_id = ?",
        (key_id,)
    )
    if peer_row and peer_row['private_key'] and key_id == recorded_by:
        log.debug(f"get_key_by_id() found peer private key for peer_id={key_id}")
        return {
            'id': id_bytes,
            'private_key': peer_row['private_key'],
            'type': 'asymmetric'
        }

    log.debug(f"get_key_by_id() peer private key not found for peer_id={key_id}")
    log.warning(f"get_key_by_id() NO KEY FOUND ANYWHERE for key_id={key_id}, recorded_by={recorded_by[:20]}...")
    return None


def get_key(key_id: str, recorded_by: str, db: Any) -> dict[str, Any]:
    """Get key from database in format expected by crypto.wrap().

    NOTE: This function is ONLY used for wrapping/encrypting events (see usage in user.py,
    group.py, channel.py, etc.). Wrapping is a "public" operation - anyone with the key
    material can encrypt to it. Access control for unwrapping/decrypting happens in
    get_key_by_id() via crypto.unwrap().

    Args:
        key_id: Base64-encoded key ID (event ID for regular keys)
        recorded_by: Peer ID requesting access (for logging, not enforced for wrapping)
        db: Database connection

    Returns:
        Key dict for crypto.wrap()

    Raises:
        ValueError: If key not found in keys table
    """
    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT key FROM keys WHERE key_id = ?", (key_id,))
    if not row:
        raise ValueError(f"key not found: {key_id}")

    # Use event ID as hint (not hash of key material)
    # The hint is prepended to wrapped blobs for lookup during unwrap
    key_material = row['key']

    return {
        'id': crypto.b64decode(key_id),  # Event ID as hint (for blob prefix)
        'key': key_material,  # Already bytes from DB
        'type': 'symmetric'
    }


def get_peer_ids_for_key(key_id: str, db: Any) -> list[str]:
    """Get ALL peer_ids that have access to a specific key.

    SECURITY NOTE: This function intentionally lacks recorded_by parameter because
    it's used for ROUTING, not access control. Called by sync.unwrap_and_store() to
    determine which local peer(s) can decrypt incoming blobs. The key_id comes from
    network data (blob headers), not user input. Does not expose private data TO the
    caller - determines WHO should receive data. Safe for internal routing logic.

    This handles the edge case where multiple local peers have the same symmetric key
    (e.g., two peers in the same network both accepted the same invite).

    Args:
        key_id: Base64-encoded key ID (hint from wrapped blob)
        db: Database connection

    Returns:
        List of peer IDs (may be empty if key not found)
    """
    # First check key_ownership table for symmetric key routing
    # Supports multiple local peers having the same symmetric key
    unsafedb = create_unsafe_db(db)
    ownership_rows = unsafedb.query(
        "SELECT peer_id FROM key_ownership WHERE key_id = ?",
        (key_id,)
    )
    if ownership_rows:
        return [row['peer_id'] for row in ownership_rows]

    # If not found in store, check if this key_id IS a prekey_shared_id (for asymmetric prekeys)
    # Prekey-wrapped blobs use prekey_shared_id (event ID) as the hint
    # Look up which local peer OWNS this prekey (has the private key)
    # NOTE: Only the owner can decrypt blobs wrapped to this prekey
    # Use raw connection to get cross-peer routing info (safe for routing)
    prekey_owners = db._conn.execute(
        "SELECT owner_peer_id FROM prekeys WHERE prekey_shared_id = ?",
        (key_id,)
    ).fetchall()
    if prekey_owners:
        return [row[0] for row in prekey_owners]

    return []


def get_peer_id_for_key(key_id: str, db: Any) -> str:
    """Get the peer_id that owns a specific key.

    SECURITY NOTE: Like get_peer_ids_for_key(), this function is used for internal
    routing logic, not access control. Safe because it doesn't expose private data
    to the caller - just determines ownership for routing purposes.

    Args:
        key_id: Base64-encoded key ID (hint from wrapped blob)
        db: Database connection

    Returns:
        Peer ID string, or empty string if not found
    """
    peer_ids = get_peer_ids_for_key(key_id, db)
    return peer_ids[0] if peer_ids else ""
