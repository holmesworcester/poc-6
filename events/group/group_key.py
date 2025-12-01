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

    # Create DETERMINISTIC event blob - only type and key
    # This ensures same key material = same key_id on all peers
    event_data = {
        'type': 'group_key',
        'key': crypto.b64encode(key)
    }

    # Use sort_keys=True for canonical ordering
    blob = json.dumps(event_data, sort_keys=True).encode()

    # Store event with recorded wrapper and projection
    # t_ms is used for recorded_at metadata, not in the blob itself
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_key.create() created key_id={key_id}")
    return key_id


def create_with_material(key_material: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create group key event with provided key material (for invite group keys).

    Creates a DETERMINISTIC group_key event from the key material.
    Same key_material = same key_id on all peers.

    Args:
        key_material: The symmetric key bytes
        peer_id: Peer ID that owns this key
        t_ms: Timestamp (used for recorded_at, NOT in the blob)
        db: Database connection

    Returns:
        Event ID (to use as hint when wrapping)
    """
    log.info(f"group_key.create_with_material() creating key for peer_id={peer_id}, t_ms={t_ms}")

    # Create DETERMINISTIC event blob - only type and key
    # This ensures same key material = same key_id on all peers
    event_data = {
        'type': 'group_key',
        'key': crypto.b64encode(key_material)
    }

    # Use sort_keys=True for canonical ordering
    blob = json.dumps(event_data, sort_keys=True).encode()
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_key.create_with_material() created key_id={key_id}")
    return key_id


def project(key_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project group key event using pure projector.

    Uses apply_result since group_keys is subjective (has recorded_by).
    Note: created_at comes from recorded_at (deterministic blobs have no timestamp).
    """
    from projectors import resolve, apply_result
    from projectors import group_key as gk_projector

    log.debug(f"group_key.project() projecting key_id={key_id}, seen_by={recorded_by}")

    input_dict = resolve("group_key", key_id, recorded_by, recorded_at, db)
    if not input_dict:
        log.warning(f"group_key.project() resolve failed for key_id={key_id}")
        return

    result = gk_projector.project(input_dict)

    if not result.valid:
        log.warning(f"group_key.project() rejected: {result.reason}")
        return

    # Apply to subjective table
    apply_result(result, recorded_by, recorded_at, db)

    # Mark as valid for this peer
    safedb = create_safe_db(db, recorded_by=recorded_by)
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


def get_or_create_clean_key(group_id: str, peer_id: str, t_ms: int, db: Any) -> str:
    """Get an existing clean key or create a new one if needed.

    A "clean" key is one NOT in the keys_to_purge table (not encrypting deleted messages).
    Used during forward secrecy rekeying to find a key safe to re-encrypt with.

    Args:
        group_id: Group that owns the key
        peer_id: Local peer ID
        t_ms: Timestamp for creating new key if needed
        db: Database connection

    Returns:
        key_id: A clean group key suitable for rekeying

    Raises:
        ValueError: If no group key exists and cannot create one
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Find an existing clean key (not in keys_to_purge)
    clean_key_row = safedb.query_one(
        """SELECT gk.key_id FROM group_keys gk
           LEFT JOIN keys_to_purge ktp ON gk.key_id = ktp.key_id AND ktp.recorded_by = ?
           WHERE gk.recorded_by = ? AND ktp.key_id IS NULL
           ORDER BY gk.created_at DESC
           LIMIT 1""",
        (peer_id, peer_id)
    )

    if clean_key_row:
        key_id = clean_key_row['key_id']
        log.info(f"group_key.get_or_create_clean_key() found existing clean key {key_id[:20]}...")
        return key_id

    # No clean key exists, create a new one
    log.info(f"group_key.get_or_create_clean_key() no clean key found, creating new one")
    key_id = create(peer_id, t_ms, db)
    log.info(f"group_key.get_or_create_clean_key() created new key {key_id[:20]}...")
    return key_id
