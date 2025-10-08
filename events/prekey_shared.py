"""Prekey shared event type (shareable public prekey)."""
from typing import Any
import logging
import crypto
import store
from events import peer, key
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(prekey_id: str, peer_id: str, peer_shared_id: str,
           group_id: str, key_id: str, t_ms: int, db: Any,
           wrap_key_data: dict | None = None) -> str:
    """Create a shareable prekey_shared event from a local prekey.

    Args:
        prekey_id: Local prekey event ID (to get public key from)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        group_id: Group context (for access control)
        key_id: Key to encrypt the prekey_shared event with
        t_ms: Timestamp
        db: Database connection
        wrap_key_data: Optional key dict for wrapping (used when network key not available yet)

    Returns:
        prekey_shared_id: The stored prekey_shared event ID
    """
    log.info(f"prekey_shared.create() creating prekey_shared for prekey_id={prekey_id}, t_ms={t_ms}")

    # Get public key from local prekey event
    prekey_blob = store.get(prekey_id, db)
    if not prekey_blob:
        raise ValueError(f"prekey not found: {prekey_id}")

    prekey_data = crypto.parse_json(prekey_blob)
    prekey_public_b64 = prekey_data['public_key']

    # Create shareable event (encrypted + signed)
    # Include prekey_id for linking back during projection
    event_data = {
        'type': 'prekey_shared',
        'prekey_id': prekey_id,
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

    # Get key_data for encryption (use provided wrap_key_data or fetch from keys table)
    if wrap_key_data:
        key_data = wrap_key_data
    else:
        key_data = key.get_key(key_id, peer_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event with recorded wrapper and projection
    prekey_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"prekey_shared.create() created prekey_shared_id={prekey_shared_id}")
    return prekey_shared_id




def project(prekey_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project prekey_shared event into pre_keys table and shareable_events."""
    log.info(f"prekey_shared.project() prekey_shared_id={prekey_shared_id}, seen_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(prekey_shared_id, unsafedb)
    if not blob:
        log.warning(f"prekey_shared.project() blob not found for prekey_shared_id={prekey_shared_id}")
        return None

    # Unwrap (decrypt)
    unwrapped, _ = crypto.unwrap(blob, recorded_by, db)
    if not unwrapped:
        log.warning(f"prekey_shared.project() failed to unwrap prekey_shared_id={prekey_shared_id}")
        return None

    # Parse JSON
    event_data = crypto.parse_json(unwrapped)

    # Verify signature - get public key from created_by peer_shared
    from events import peer_shared
    created_by = event_data['created_by']
    public_key = peer_shared.get_public_key(created_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"prekey_shared.project() signature verification failed for prekey_shared_id={prekey_shared_id}")
        return None

    # Insert into prekeys_shared table
    prekey_public = crypto.b64decode(event_data['public_key'])
    safedb.execute(
        """INSERT OR IGNORE INTO prekeys_shared
           (prekey_shared_id, peer_id, public_key, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            prekey_shared_id,
            event_data['peer_id'],
            prekey_public,
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )

    # Link prekey_shared_id back to local prekey if it exists
    # This enables reprojection to restore the link
    if 'prekey_id' in event_data:
        prekey_id = event_data['prekey_id']
        log.info(f"prekey_shared.project() linking prekey_shared_id={prekey_shared_id} to prekey_id={prekey_id}, owner={recorded_by[:20]}...")
        unsafedb = create_unsafe_db(db)
        # Only update if we have this prekey locally (owner)
        prekey_exists = unsafedb.query_one(
            "SELECT 1 FROM prekeys WHERE prekey_id = ? AND owner_peer_id = ?",
            (prekey_id, recorded_by)
        )
        if prekey_exists:
            log.info(f"prekey_shared.project() prekey exists, updating prekey_shared_id")
            cursor = unsafedb.execute(
                "UPDATE prekeys SET prekey_shared_id = ? WHERE prekey_id = ? AND owner_peer_id = ?",
                (prekey_shared_id, prekey_id, recorded_by)
            )
            rows_updated = cursor.rowcount if hasattr(cursor, 'rowcount') else 'unknown'
            log.info(f"prekey_shared.project() UPDATE completed, rows_updated={rows_updated}")

            # Verify the update worked
            verify_row = unsafedb.query_one(
                "SELECT prekey_shared_id FROM prekeys WHERE prekey_id = ? AND owner_peer_id = ?",
                (prekey_id, recorded_by)
            )
            if verify_row:
                log.info(f"prekey_shared.project() VERIFIED: prekey_shared_id in DB is now {verify_row['prekey_shared_id']}")
            else:
                log.error(f"prekey_shared.project() VERIFICATION FAILED: could not find prekey after update")
        else:
            log.warning(f"prekey_shared.project() prekey NOT FOUND in prekeys table for prekey_id={prekey_id}, owner={recorded_by[:20]}...")
    else:
        log.warning(f"prekey_shared.project() NO prekey_id in event_data for prekey_shared_id={prekey_shared_id}")

    # Mark as valid for this peer
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (prekey_shared_id, recorded_by)
    )

    return prekey_shared_id
