"""Prekey shared event type (shareable public prekey)."""
from typing import Any
import logging
import crypto
import store
from events.transit import transit_key
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(prekey_id: str, peer_id: str, peer_shared_id: str,
           group_id: str, key_id: str, t_ms: int, db: Any,
           wrap_key_data: dict | None = None) -> str:
    """Create a shareable group_prekey_shared event from a local group prekey.

    Args:
        prekey_id: Local group_prekey event ID (to get public key from)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        group_id: Group context (for access control)
        key_id: Key to encrypt the group_prekey_shared event with
        t_ms: Timestamp
        db: Database connection
        wrap_key_data: Optional key dict for wrapping (used when network key not available yet)

    Returns:
        group_prekey_shared_id: The stored group_prekey_shared event ID
    """
    log.info(f"group_prekey_shared.create() creating group_prekey_shared for prekey_id={prekey_id}, t_ms={t_ms}")

    # Get public key from local prekey event
    prekey_blob = store.get(prekey_id, db)
    if not prekey_blob:
        raise ValueError(f"prekey not found: {prekey_id}")

    prekey_data = crypto.parse_json(prekey_blob)
    prekey_public_b64 = prekey_data['public_key']

    # Create shareable event (encrypted + signed)
    # Include group_prekey_id for linking back during projection
    event_data = {
        'type': 'group_prekey_shared',
        'group_prekey_id': prekey_id,
        'peer_id': peer_shared_id,
        'group_id': group_id,
        'public_key': prekey_public_b64,
        'created_by': peer_shared_id,
        'created_at': t_ms,
        'key_id': key_id
    }

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext (no inner encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    group_prekey_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_prekey_shared.create() created group_prekey_shared_id={group_prekey_shared_id}")
    return group_prekey_shared_id




def project(group_prekey_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project group_prekey_shared event into group_prekeys_shared table and shareable_events."""
    log.info(f"group_prekey_shared.project() group_prekey_shared_id={group_prekey_shared_id}, seen_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(group_prekey_shared_id, unsafedb)
    if not blob:
        log.warning(f"group_prekey_shared.project() blob not found for group_prekey_shared_id={group_prekey_shared_id}")
        return None

    # Parse JSON (signed plaintext, no decryption needed)
    event_data = crypto.parse_json(blob)

    # Verify signature - get public key from created_by peer_shared
    from events.identity import peer_shared
    created_by = event_data['created_by']
    public_key = peer_shared.get_public_key(created_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"group_prekey_shared.project() signature verification failed for group_prekey_shared_id={group_prekey_shared_id}")
        return None

    # Insert into group_prekeys_shared table
    prekey_public = crypto.b64decode(event_data['public_key'])
    safedb.execute(
        """INSERT OR IGNORE INTO group_prekeys_shared
           (group_prekey_shared_id, peer_id, public_key, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            group_prekey_shared_id,
            event_data['peer_id'],
            prekey_public,
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )

    # Mark as valid for this peer
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (group_prekey_shared_id, recorded_by)
    )

    return group_prekey_shared_id
