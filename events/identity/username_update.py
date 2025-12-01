"""Username update event type (encrypted name update for users)."""

# Registry metadata
EVENT_TYPE = 'username_update'
SHAREABLE = True  # Username updates sync across network
EPHEMERAL = False
PROJECTION_TABLE = None

from typing import Any
import logging
import crypto
import store
from events.group import group
from events.identity import peer
from db import create_safe_db, create_unsafe_db

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
        ValueError: If user doesn't exist
    """
    log.info(f"username_update.create() creating username for user_id={user_id[:20]}..., name='{name}'")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Check: Does the user event exist?
    # We do this just for validation - the actual validation gate will happen in validate()
    user_event = safedb.query_one(
        "SELECT user_id FROM users WHERE user_id = ? AND recorded_by = ? LIMIT 1",
        (user_id, peer_id)
    )
    if not user_event:
        log.warning(f"username_update.create() user not found: {user_id[:20]}...")
        raise ValueError(f"User {user_id} not found")

    # Get all_members group
    all_members_group = safedb.query_one(
        "SELECT group_id FROM groups WHERE name = 'all_members' AND recorded_by = ? LIMIT 1",
        (peer_id,)
    )
    if not all_members_group:
        # all_members group not available yet (will come from sync)
        log.info(f"username_update.create() all_members group not found yet")
        raise KeyNotAvailableError("all_members group not available yet - will be created on sync")

    group_id = all_members_group['group_id']

    # Get the latest known key for all_members
    try:
        key_data = group.pick_key(group_id, peer_id, db)
    except Exception as e:
        log.warning(f"username_update.create() no key available: {e}")
        raise KeyNotAvailableError(f"No group key available for encryption: {e}")

    if not key_data:
        raise KeyNotAvailableError("No group key available for all_members group")

    # Extract key_id from key_data
    key_id = key_data.get('key_id')
    if not key_id:
        raise KeyNotAvailableError("Key ID not found in group key data")

    # Build username_update event
    # Note: We don't store user_id as field name since design doc uses descriptive field names
    event_data = {
        'type': 'username_update',
        'user_id': user_id,  # Identifies which user this name is for
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
    username_update_id = store.event(blob, peer_id, t_ms, db)

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

    # Parse event
    try:
        event_data = crypto.parse_json(blob)
    except Exception as e:
        log.warning(f"username_update.project() failed to parse event: {e}")
        return None

    # Extract fields
    user_id = event_data.get('user_id')
    encrypted_name = event_data.get('name')
    key_id = event_data.get('key_id')
    global_count = event_data.get('global_count', 0)

    if not user_id or not encrypted_name:
        log.warning(f"username_update.project() missing required fields")
        return None

    # Try to decrypt the name
    try:
        decrypted_name = crypto.decrypt_to_local(encrypted_name, db)

        if decrypted_name is not None:
            # We have the key, store decrypted name
            log.info(f"username_update.project() decrypted username for {user_id[:20]}...: {decrypted_name}")
            safedb.execute(
                """INSERT OR REPLACE INTO user_names
                   (user_id, name, event_id, global_count, key_id, created_at, signed_by, recorded_by, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   WHERE global_count < ? OR user_id NOT IN (SELECT user_id FROM user_names)""",
                (user_id, decrypted_name, event_id, global_count, key_id,
                 event_data.get('created_at'), event_data.get('signed_by'),
                 recorded_by, recorded_at, global_count)
            )
            log.info(f"username_update.project() stored decrypted username")
        else:
            # Key not available yet - store encrypted blob in pending table
            log.info(f"username_update.project() key not available yet, storing in pending table")
            import hashlib
            pending_id = hashlib.sha256(f"{event_id}:{recorded_by}".encode()).hexdigest()[:20]

            safedb.execute(
                """INSERT OR IGNORE INTO pending_name_decrypts
                   (id, type, entity_id, event_id, encrypted_blob, key_id, status, created_at, recorded_by, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pending_id, 'username', user_id, event_id, encrypted_name, key_id,
                 'waiting_for_key', event_data.get('created_at'), recorded_by, recorded_at)
            )
            log.info(f"username_update.project() stored in pending_name_decrypts")

    except Exception as e:
        log.warning(f"username_update.project() decryption error: {e}")
        # Even if decryption fails, store the encrypted blob
        import hashlib
        pending_id = hashlib.sha256(f"{event_id}:{recorded_by}".encode()).hexdigest()[:20]

        safedb.execute(
            """INSERT OR IGNORE INTO pending_name_decrypts
               (id, type, entity_id, event_id, encrypted_blob, key_id, status, created_at, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pending_id, 'username', user_id, event_id, encrypted_name, key_id,
             'waiting_for_key', event_data.get('created_at'), recorded_by, recorded_at)
        )

    # Mark event as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (event_id, recorded_by)
    )

    return event_id


class KeyNotAvailableError(Exception):
    """Raised when group key is not available for encryption."""
    pass
