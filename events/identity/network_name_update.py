"""Network name update event type (encrypted name update for networks)."""

# Registry metadata
EVENT_TYPE = 'network_name_update'
SHAREABLE = True  # Name updates sync across network
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


def create(network_id: str, name: str, peer_id: str, peer_shared_id: str, t_ms: int,
           db: Any) -> str:
    """Create a network_name_update event.

    The network name is encrypted to the all_members group using the latest known key.
    If the key is not available, raises KeyNotAvailableError.

    Args:
        network_id: The network event ID this name updates
        name: Plaintext network name to encrypt
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        network_name_update_id: The stored event ID

    Raises:
        KeyNotAvailableError: If all_members group key is not available yet
        ValueError: If network doesn't exist
    """
    log.info(f"network_name_update.create() creating network name for network_id={network_id[:20]}..., name='{name}'")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Check: Does the network event exist?
    network_event = safedb.query_one(
        "SELECT network_id FROM networks WHERE network_id = ? AND recorded_by = ? LIMIT 1",
        (network_id, peer_id)
    )
    if not network_event:
        log.warning(f"network_name_update.create() network not found: {network_id[:20]}...")
        raise ValueError(f"Network {network_id} not found")

    # Get main group (all_members) - use is_main flag since name varies
    main_group = safedb.query_one(
        "SELECT group_id FROM groups WHERE is_main = 1 AND recorded_by = ? LIMIT 1",
        (peer_id,)
    )
    if not main_group:
        # Main group not available yet (will come from sync)
        log.info(f"network_name_update.create() main group not found yet")
        raise KeyNotAvailableError("Main group not available yet - will be created on sync")

    group_id = main_group['group_id']

    # Get the latest known key for all_members
    try:
        key_data = group.pick_key(group_id, peer_id, db)
    except Exception as e:
        log.warning(f"network_name_update.create() no key available: {e}")
        raise KeyNotAvailableError(f"No group key available for encryption: {e}")

    if not key_data:
        raise KeyNotAvailableError("No group key available for all_members group")

    # Extract key_id from key_data - key_data has {'id': bytes, 'key': bytes}
    key_id_bytes = key_data.get('id')
    if not key_id_bytes:
        raise KeyNotAvailableError("Key ID not found in group key data")
    key_id = crypto.b64encode(key_id_bytes)

    # Build network_name_update event
    event_data = {
        'type': 'network_name_update',
        'network_id': network_id,  # Identifies which network this name is for
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
    network_name_update_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"network_name_update.create() created network_name_update_id={network_name_update_id[:20]}...")
    return network_name_update_id


def validate(event_id: str, recorded_by: str, db: Any) -> str | None:
    """Validate a network_name_update event.

    Validation checks:
    1. Does the network event (with event_id = network_id field) exist?

    Args:
        event_id: The network_name_update event ID
        recorded_by: The peer that recorded this event
        db: Database connection

    Returns:
        'VALID' if network exists and valid
        'BLOCKED' if network doesn't exist yet (wait for network event)
        None if invalid (can't recover)
    """
    log.debug(f"network_name_update.validate() validating {event_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get the event blob to extract network_id field
    blob = store.get(event_id, db)
    if not blob:
        log.warning(f"network_name_update.validate() blob not found for {event_id[:20]}...")
        return None

    try:
        # Parse the event
        event_data = crypto.parse_json(blob)
    except Exception as e:
        log.warning(f"network_name_update.validate() failed to parse event: {e}")
        return None

    # Extract network_id field
    network_id = event_data.get('network_id')
    if not network_id:
        log.warning(f"network_name_update.validate() missing network_id field")
        return None

    # Check: Does the network event exist?
    network_event = safedb.query_one(
        "SELECT network_id FROM networks WHERE network_id = ? AND recorded_by = ? LIMIT 1",
        (network_id, recorded_by)
    )

    if not network_event:
        log.debug(f"network_name_update.validate() network not found, blocking: {network_id[:20]}...")
        return "BLOCKED"

    log.debug(f"network_name_update.validate() valid: network exists")
    return "VALID"


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project a network_name_update event into the database.

    Projection logic:
    1. Get the event blob and decrypt it
    2. Extract network_id and name
    3. Try to decrypt the name
    4. If decryption succeeds: store in network_names table
    5. If decryption fails (key missing): store encrypted blob in pending_name_decrypts table

    Args:
        event_id: The network_name_update event ID
        recorded_by: The peer that recorded this event
        recorded_at: When this was recorded
        db: Database connection

    Returns:
        event_id if projection succeeded, None otherwise
    """
    log.debug(f"network_name_update.project() projecting {event_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob
    blob = store.get(event_id, db)
    if not blob:
        log.warning(f"network_name_update.project() blob not found")
        return None

    # Parse event
    try:
        event_data = crypto.parse_json(blob)
    except Exception as e:
        log.warning(f"network_name_update.project() failed to parse event: {e}")
        return None

    # Extract fields
    network_id = event_data.get('network_id')
    encrypted_name = event_data.get('name')
    key_id = event_data.get('key_id')
    global_count = event_data.get('global_count', 0)

    if not network_id or not encrypted_name:
        log.warning(f"network_name_update.project() missing required fields")
        return None

    # Try to decrypt the name
    try:
        decrypted_name = crypto.decrypt_to_local(encrypted_name, db)

        if decrypted_name is not None:
            # We have the key, store decrypted name with LWW logic
            log.info(f"network_name_update.project() decrypted network name for {network_id[:20]}...: {decrypted_name}")

            # Check if entry already exists
            existing = safedb.query_one(
                "SELECT global_count FROM network_names WHERE network_id = ? AND recorded_by = ?",
                (network_id, recorded_by)
            )

            if existing is None:
                # No existing entry - insert new
                safedb.execute(
                    """INSERT INTO network_names
                       (network_id, name, event_id, global_count, key_id, created_at, signed_by, recorded_by, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (network_id, decrypted_name, event_id, global_count, key_id,
                     event_data.get('created_at'), event_data.get('signed_by'),
                     recorded_by, recorded_at)
                )
            elif existing['global_count'] < global_count:
                # Existing entry has lower count - update with LWW
                safedb.execute(
                    """UPDATE network_names
                       SET name = ?, event_id = ?, global_count = ?, key_id = ?,
                           created_at = ?, signed_by = ?, recorded_at = ?
                       WHERE network_id = ? AND recorded_by = ?""",
                    (decrypted_name, event_id, global_count, key_id,
                     event_data.get('created_at'), event_data.get('signed_by'),
                     recorded_at, network_id, recorded_by)
                )
            # else: existing has higher or equal count - skip (LWW)

            log.info(f"network_name_update.project() stored decrypted network name")
        else:
            # Key not available yet - store encrypted blob in pending table
            log.info(f"network_name_update.project() key not available yet, storing in pending table")
            import hashlib
            pending_id = hashlib.sha256(f"{event_id}:{recorded_by}".encode()).hexdigest()[:20]

            safedb.execute(
                """INSERT OR IGNORE INTO pending_name_decrypts
                   (id, type, entity_id, event_id, encrypted_blob, key_id, status, created_at, recorded_by, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pending_id, 'network_name', network_id, event_id, encrypted_name, key_id,
                 'waiting_for_key', event_data.get('created_at'), recorded_by, recorded_at)
            )
            log.info(f"network_name_update.project() stored in pending_name_decrypts")

    except Exception as e:
        log.warning(f"network_name_update.project() decryption error: {e}")
        # Even if decryption fails, store the encrypted blob
        import hashlib
        pending_id = hashlib.sha256(f"{event_id}:{recorded_by}".encode()).hexdigest()[:20]

        safedb.execute(
            """INSERT OR IGNORE INTO pending_name_decrypts
               (id, type, entity_id, event_id, encrypted_blob, key_id, status, created_at, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pending_id, 'network_name', network_id, event_id, encrypted_name, key_id,
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
