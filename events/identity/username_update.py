"""Username update event type (encrypted name update for users)."""

# Registry metadata
EVENT_TYPE = 'username_update'
SHAREABLE = True  # Username updates sync across network
EPHEMERAL = False
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from events.group import group
from core.db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(user_id: str, name: str, peer_id: str, peer_shared_id: str, t_ms: int,
           db: Any) -> str:
    """Create a username_update event.

    The username is encrypted to the all_members group using the latest known key.
    If the key is not available, raises KeyNotAvailableError.

    Args:
        user_id: The user event ID this name updates
        name: Plaintext username to encrypt
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        username_update_id: The stored event ID

    Raises:
        KeyNotAvailableError: If all_members group key is not available yet
    """
    log.info(f"username_update.create() creating username for user_id={user_id[:20]}..., name='{name}'")

    # NOTE: user_id is passed as param - no need to check users table
    # The event will have user_id as a dependency and will block until user is valid

    # Get main group (all_members) - use is_main flag since name varies
    main_group = group.get_main(peer_id, db)
    if not main_group:
        # Main group not available yet (will come from sync)
        log.info(f"username_update.create() main group not found yet")
        raise KeyNotAvailableError("Main group not available yet - will be created on sync")

    group_id = main_group['group_id']

    # Get the latest known key for all_members
    try:
        key_data = group.pick_key(group_id, peer_id, db)
    except Exception as e:
        log.warning(f"username_update.create() no key available: {e}")
        raise KeyNotAvailableError(f"No group key available for encryption: {e}")

    if not key_data:
        raise KeyNotAvailableError("No group key available for all_members group")

    # Extract key_id from key_data to include in event
    key_id_bytes = key_data.get('id')
    if not key_id_bytes:
        raise KeyNotAvailableError("Key ID not found in group key data")
    key_id = crypto.b64encode(key_id_bytes)

    # Build username_update event
    event_data = {
        'type': 'username_update',
        'user_id': user_id,
        'name': name,
        'key_id': key_id,
        'global_count': 0,
        'signed_by': peer_shared_id,
        'created_at': t_ms
    }

    username_update_id = store.publish(event_data, group_id, peer_id, t_ms, db)

    log.info(f"username_update.create() created username_update_id={username_update_id[:20]}...")
    return username_update_id


def validate(event_id: str, recorded_by: str, db: Any) -> str | None:
    """Validate a username_update event.

    Validation checks:
    1. Does the user event (with event_id = user_id field) exist?

    Args:
        event_id: The username_update event ID
        recorded_by: The peer that recorded this event
        db: Database connection

    Returns:
        'VALID' if user exists and valid
        'BLOCKED' if user doesn't exist yet (wait for user event)
        None if invalid (can't recover)
    """
    log.debug(f"username_update.validate() validating {event_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get the event blob to extract user_id field
    blob = store.get(event_id, db)
    if not blob:
        log.warning(f"username_update.validate() blob not found for {event_id[:20]}...")
        return None

    try:
        # Parse the event (wrapped blob)
        # The wrap/unwrap is handled by crypto.parse_json if it's a wrapped blob
        event_data = crypto.parse_json(blob)
    except Exception as e:
        log.warning(f"username_update.validate() failed to parse event: {e}")
        return None

    # Extract user_id field
    user_id = event_data.get('user_id')
    if not user_id:
        log.warning(f"username_update.validate() missing user_id field")
        return None

    # Check: Does the user event exist?
    user_event = safedb.query_one(
        "SELECT type FROM users WHERE user_id = ? AND recorded_by = ? LIMIT 1",
        (user_id, recorded_by)
    )

    if not user_event:
        log.debug(f"username_update.validate() user not found, blocking: {user_id[:20]}...")
        return "BLOCKED"

    log.debug(f"username_update.validate() valid: user exists")
    return "VALID"


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project a username_update event into the database.

    Projection logic:
    1. Get the event blob and decrypt it
    2. Extract user_id and name
    3. Try to decrypt the name
    4. If decryption succeeds: store in user_names table
    5. If decryption fails (key missing): store encrypted blob in pending_name_decrypts table

    Args:
        event_id: The username_update event ID
        recorded_by: The peer that recorded this event
        recorded_at: When this was recorded
        db: Database connection

    Returns:
        event_id if projection succeeded, None otherwise
    """
    log.debug(f"username_update.project() projecting {event_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob
    blob = store.get(event_id, db)
    if not blob:
        log.warning(f"username_update.project() blob not found")
        return None

    # Unwrap event (decrypt)
    try:
        unwrapped, missing_keys = crypto.unwrap(blob, recorded_by, db)
        if not unwrapped:
            if missing_keys:
                # Key not available yet - store in pending table
                log.info(f"username_update.project() key not available yet (missing: {missing_keys})")
                import hashlib
                pending_id = hashlib.sha256(f"{event_id}:{recorded_by}".encode()).hexdigest()[:20]
                key_id = missing_keys[0] if missing_keys else None

                safedb.execute(
                    """INSERT OR IGNORE INTO pending_name_decrypts
                       (id, type, entity_id, event_id, encrypted_blob, key_id, status, created_at, recorded_by, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (pending_id, 'username', None, event_id, blob, key_id,
                     'waiting_for_key', recorded_at, recorded_by, recorded_at)
                )
                return None
            log.warning(f"username_update.project() unwrap failed")
            return None
        event_data = crypto.parse_json(unwrapped)
    except Exception as e:
        log.warning(f"username_update.project() failed to unwrap/parse event: {e}")
        return None

    # Verify signature before trusting event data
    if not crypto.verify_signed_by_peer_shared(event_data, recorded_by, db):
        log.warning(f"username_update.project() signature verification failed for {event_id[:20]}...")
        return None

    # Extract fields - name is already plaintext after unwrap
    user_id = event_data.get('user_id')
    name = event_data.get('name')
    key_id = event_data.get('key_id')
    global_count = event_data.get('global_count', 0)

    if not user_id or not name:
        log.warning(f"username_update.project() missing required fields")
        return None

    # Store the decrypted name with LWW logic
    log.info(f"username_update.project() storing username for {user_id[:20]}...: {name}")

    # Check if entry already exists
    existing = safedb.query_one(
        "SELECT global_count FROM user_names WHERE user_id = ? AND recorded_by = ?",
        (user_id, recorded_by)
    )

    if existing is None:
        # No existing entry - insert new
        safedb.execute(
            """INSERT INTO user_names
               (user_id, name, event_id, global_count, key_id, created_at, signed_by, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name, event_id, global_count, key_id,
             event_data.get('created_at'), event_data.get('signed_by'),
             recorded_by, recorded_at)
        )
    elif existing['global_count'] < global_count:
        # Existing entry has lower count - update with LWW
        safedb.execute(
            """UPDATE user_names
               SET name = ?, event_id = ?, global_count = ?, key_id = ?,
                   created_at = ?, signed_by = ?, recorded_at = ?
               WHERE user_id = ? AND recorded_by = ?""",
            (name, event_id, global_count, key_id,
             event_data.get('created_at'), event_data.get('signed_by'),
             recorded_at, user_id, recorded_by)
        )
    # else: existing has higher or equal count - skip (LWW)

    log.info(f"username_update.project() stored username")

    # Mark event as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (event_id, recorded_by)
    )

    return event_id


class KeyNotAvailableError(Exception):
    """Raised when group key is not available for encryption."""
    pass
