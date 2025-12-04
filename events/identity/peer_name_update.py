"""Peer name update event type (encrypted name update for peers/devices)."""

# Registry metadata
EVENT_TYPE = 'peer_name_update'
SHAREABLE = True  # Peer name updates sync across network
EPHEMERAL = False
PROJECTION_TABLE = None

from typing import Any
import logging
import crypto
import store
from events.group import group
from events.identity import peer
from db import create_safe_db

log = logging.getLogger(__name__)


def create(peer_target_id: str, name: str, peer_id: str, peer_shared_id: str, t_ms: int,
           db: Any) -> str:
    """Create a peer_name_update event.

    The peer name is encrypted to the all_members group using the latest known key.
    If the key is not available, raises KeyNotAvailableError.

    Args:
        peer_target_id: The peer (device) this name updates
        name: Plaintext peer name to encrypt
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        peer_name_update_id: The stored event ID

    Raises:
        KeyNotAvailableError: If all_members group key is not available yet
    """
    log.info(f"peer_name_update.create() creating peer name for peer_target_id={peer_target_id[:20]}..., name='{name}'")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # NOTE: peer_shared_id is passed as param - no need to check peers_shared table
    # The event will have peer_target_id as a dependency and will block until peer_shared is valid

    # Get main group (all_members) - use is_main flag since name varies
    main_group = safedb.query_one(
        "SELECT group_id FROM groups WHERE is_main = 1 AND recorded_by = ? LIMIT 1",
        (peer_id,)
    )
    if not main_group:
        # Main group not available yet (will come from sync)
        log.info(f"peer_name_update.create() main group not found yet")
        raise KeyNotAvailableError("Main group not available yet - will be created on sync")

    group_id = main_group['group_id']

    # Get the latest known key for all_members
    try:
        key_data = group.pick_key(group_id, peer_id, db)
    except Exception as e:
        log.warning(f"peer_name_update.create() no key available: {e}")
        raise KeyNotAvailableError(f"No group key available for encryption: {e}")

    if not key_data:
        raise KeyNotAvailableError("No group key available for all_members group")

    # Extract key_id from key_data - key_data has {'id': bytes, 'key': bytes}
    key_id_bytes = key_data.get('id')
    if not key_id_bytes:
        raise KeyNotAvailableError("Key ID not found in group key data")
    key_id = crypto.b64encode(key_id_bytes)

    # Build peer_name_update event
    event_data = {
        'type': 'peer_name_update',
        'peer_id': peer_target_id,  # Identifies which peer this name is for
        'name': name,  # Will be encrypted
        'key_id': key_id,  # Track which key was used
        'global_count': 0,  # For LWW (last-writer-wins)
        'signed_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Encrypt to group (using key_data for wrapping)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event with recorded wrapper
    peer_name_update_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"peer_name_update.create() created peer_name_update_id={peer_name_update_id[:20]}...")
    return peer_name_update_id


def validate(event_id: str, recorded_by: str, db: Any) -> str | None:
    """Validate a peer_name_update event.

    Validation checks:
    1. Does the peer event (with event_id = peer_id field) exist?

    Args:
        event_id: The peer_name_update event ID
        recorded_by: The peer that recorded this event
        db: Database connection

    Returns:
        'VALID' if peer exists and valid
        'BLOCKED' if peer doesn't exist yet (wait for peer event)
        None if invalid (can't recover)
    """
    log.debug(f"peer_name_update.validate() validating {event_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get the event blob to extract peer_id field
    blob = store.get(event_id, db)
    if not blob:
        log.warning(f"peer_name_update.validate() blob not found for {event_id[:20]}...")
        return None

    try:
        # Parse the event
        event_data = crypto.parse_json(blob)
    except Exception as e:
        log.warning(f"peer_name_update.validate() failed to parse event: {e}")
        return None

    # Extract peer_id field
    peer_target_id = event_data.get('peer_id')
    if not peer_target_id:
        log.warning(f"peer_name_update.validate() missing peer_id field")
        return None

    # Check: Does the peer_shared event exist?
    peer_event = safedb.query_one(
        "SELECT peer_shared_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (peer_target_id, recorded_by)
    )

    if not peer_event:
        log.debug(f"peer_name_update.validate() peer not found, blocking: {peer_target_id[:20]}...")
        return "BLOCKED"

    log.debug(f"peer_name_update.validate() valid: peer exists")
    return "VALID"


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project a peer_name_update event into the database.

    Projection logic:
    1. Get the event blob and decrypt it
    2. Extract peer_id and name
    3. Try to decrypt the name
    4. If decryption succeeds: store in peer_names table
    5. If decryption fails (key missing): store encrypted blob in pending_name_decrypts table

    Args:
        event_id: The peer_name_update event ID
        recorded_by: The peer that recorded this event
        recorded_at: When this was recorded
        db: Database connection

    Returns:
        event_id if projection succeeded, None otherwise
    """
    log.debug(f"peer_name_update.project() projecting {event_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob
    blob = store.get(event_id, db)
    if not blob:
        log.warning(f"peer_name_update.project() blob not found")
        return None

    # Unwrap event (decrypt)
    try:
        unwrapped, missing_keys = crypto.unwrap(blob, recorded_by, db)
        if not unwrapped:
            if missing_keys:
                # Key not available yet - store in pending table
                log.info(f"peer_name_update.project() key not available yet (missing: {missing_keys})")
                import hashlib
                pending_id = hashlib.sha256(f"{event_id}:{recorded_by}".encode()).hexdigest()[:20]
                key_id = missing_keys[0] if missing_keys else None

                safedb.execute(
                    """INSERT OR IGNORE INTO pending_name_decrypts
                       (id, type, entity_id, event_id, encrypted_blob, key_id, status, created_at, recorded_by, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (pending_id, 'peer_name', None, event_id, blob, key_id,
                     'waiting_for_key', recorded_at, recorded_by, recorded_at)
                )
                return None
            log.warning(f"peer_name_update.project() unwrap failed")
            return None
        event_data = crypto.parse_json(unwrapped)
    except Exception as e:
        log.warning(f"peer_name_update.project() failed to unwrap/parse event: {e}")
        return None

    # Extract fields - name is already plaintext after unwrap
    peer_target_id = event_data.get('peer_id')
    name = event_data.get('name')
    key_id = event_data.get('key_id')
    global_count = event_data.get('global_count', 0)

    if not peer_target_id or not name:
        log.warning(f"peer_name_update.project() missing required fields")
        return None

    # Store the decrypted name with LWW logic
    log.info(f"peer_name_update.project() storing peer name for {peer_target_id[:20]}...: {name}")

    # Check if entry already exists
    existing = safedb.query_one(
        "SELECT global_count FROM peer_names WHERE peer_id = ? AND recorded_by = ?",
        (peer_target_id, recorded_by)
    )

    if existing is None:
        # No existing entry - insert new
        safedb.execute(
            """INSERT INTO peer_names
               (peer_id, name, event_id, global_count, key_id, created_at, signed_by, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (peer_target_id, name, event_id, global_count, key_id,
             event_data.get('created_at'), event_data.get('signed_by'),
             recorded_by, recorded_at)
        )
    elif existing['global_count'] < global_count:
        # Existing entry has lower count - update with LWW
        safedb.execute(
            """UPDATE peer_names
               SET name = ?, event_id = ?, global_count = ?, key_id = ?,
                   created_at = ?, signed_by = ?, recorded_at = ?
               WHERE peer_id = ? AND recorded_by = ?""",
            (name, event_id, global_count, key_id,
             event_data.get('created_at'), event_data.get('signed_by'),
             recorded_at, peer_target_id, recorded_by)
        )
    # else: existing has higher or equal count - skip (LWW)

    log.info(f"peer_name_update.project() stored peer name")

    # Mark event as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (event_id, recorded_by)
    )

    return event_id


class KeyNotAvailableError(Exception):
    """Raised when group key is not available for encryption."""
    pass
