"""Group key event type (subjective symmetric keys for network/group content encryption)."""
from typing import Any
import json
import logging
import crypto
import store
from db import create_safe_db

log = logging.getLogger(__name__)


def create(peer_id: str, t_ms: int, db: Any) -> str:
    """Create a group key for network content encryption, owned by peer_id."""
    log.info(f"group_key.create() creating new group key for peer_id={peer_id}, t_ms={t_ms}")

    # Generate symmetric key
    key = crypto.generate_secret()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'group_key',
        'key': crypto.b64encode(key),
        'created_by': peer_id,  # Local peer who created this key
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store event with recorded wrapper and projection
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_key.create() created key_id={key_id}")
    return key_id


def create_with_material(key_material: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create group key event with provided key material (for invite group keys).

    Args:
        key_material: The symmetric key bytes
        peer_id: Peer ID that owns this key
        t_ms: Timestamp
        db: Database connection

    Returns:
        Event ID (to use as hint when wrapping)
    """
    log.info(f"group_key.create_with_material() creating key for peer_id={peer_id}, t_ms={t_ms}")

    event_data = {
        'type': 'group_key',
        'key': crypto.b64encode(key_material),
        'created_by': peer_id,  # Local peer who created this key
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_key.create_with_material() created key_id={key_id}")
    return key_id


def project(key_id: str, recorded_by: str, db: Any) -> None:
    """Project group key event into group_keys table and mark valid for owning peer."""
    log.debug(f"group_key.project() projecting key_id={key_id}, seen_by={recorded_by}")

    # Get blob from store
    blob = store.get(key_id, db)
    if not blob:
        log.warning(f"group_key.project() blob not found for key_id={key_id}")
        return

    # Parse JSON
    event_data = crypto.parse_json(blob)

    # Insert into group_keys table (subjective)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    log.debug(f"group_key.project() inserting key_id={key_id} into group_keys table")
    safedb.execute(
        """INSERT OR IGNORE INTO group_keys (key_id, key, created_at, recorded_by)
           VALUES (?, ?, ?, ?)""",
        (
            key_id,
            crypto.b64decode(event_data['key']),
            event_data['created_at'],
            recorded_by
        )
    )

    # Mark as valid for this peer
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (key_id, recorded_by)
    )

    log.info(f"group_key.project() projected key_id={key_id} into group_keys table")


def get_key(key_id: str, recorded_by: str, db: Any) -> dict[str, Any]:
    """Get group key from database in format expected by crypto.wrap().

    Args:
        key_id: Base64-encoded key ID (event ID)
        recorded_by: Peer ID requesting access
        db: Database connection

    Returns:
        Key dict for crypto.wrap()

    Raises:
        ValueError: If key not found in group_keys table
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT key FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (key_id, recorded_by)
    )
    if not row:
        raise ValueError(f"group key not found: {key_id}")

    return {
        'id': crypto.b64decode(key_id),  # Event ID as hint
        'key': row['key'],  # Already bytes from DB
        'type': 'symmetric'
    }
