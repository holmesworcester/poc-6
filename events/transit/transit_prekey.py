"""Transit prekey event type (device-wide prekey keypair for receiving sync requests)."""
from typing import Any
import json
import logging
import crypto
import store
from db import create_unsafe_db, create_safe_db

log = logging.getLogger(__name__)


def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a device-wide transit prekey event.

    Generates Ed25519 keypair, stores both public and private keys in event.
    Projects to transit_prekeys table with owner_peer_id.

    Args:
        peer_id: Local peer ID (owner of this prekey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (prekey_id, prekey_private): The stored prekey event ID and private key bytes
    """
    log.info(f"transit_prekey.create() creating new prekey for peer_id={peer_id}, t_ms={t_ms}")

    # Generate Ed25519 keypair for prekey
    prekey_private, prekey_public = crypto.generate_keypair()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'transit_prekey',
        'public_key': crypto.b64encode(prekey_public),
        'private_key': crypto.b64encode(prekey_private),
        'created_by': peer_id,  # Local peer who created this prekey
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    unsafedb = create_unsafe_db(db)

    # Store the blob to get prekey_id
    prekey_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    log.info(f"transit_prekey.create() generated prekey_id={prekey_id}")

    # Create recorded wrapper where peer sees itself
    from events.transit import recorded
    recorded_id = recorded.create(prekey_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    log.info(f"transit_prekey.create() projected prekey_id={prekey_id}")
    return prekey_id, prekey_private


def create_with_material(public_key: bytes, private_key: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create a transit prekey event with provided key material (for invite prekeys).

    Args:
        public_key: Ed25519 public key bytes
        private_key: Ed25519 private key bytes
        peer_id: Local peer ID (owner of this prekey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        prekey_id: The stored prekey event ID
    """
    log.info(f"transit_prekey.create_with_material() creating prekey for peer_id={peer_id}, t_ms={t_ms}")

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'transit_prekey',
        'public_key': crypto.b64encode(public_key),
        'private_key': crypto.b64encode(private_key),
        'created_by': peer_id,  # Local peer who created this prekey
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    unsafedb = create_unsafe_db(db)

    # Store the blob to get prekey_id
    prekey_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    log.info(f"transit_prekey.create_with_material() generated prekey_id={prekey_id}")

    # Create recorded wrapper where peer sees itself
    from events.transit import recorded
    recorded_id = recorded.create(prekey_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    log.info(f"transit_prekey.create_with_material() projected prekey_id={prekey_id}")
    return prekey_id


def project(prekey_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project transit prekey event into transit_prekeys table with owner_peer_id."""
    log.info(f"transit_prekey.project() prekey_id={prekey_id[:30]}..., seen_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(prekey_id, unsafedb)
    if not blob:
        log.warning(f"transit_prekey.project() blob not found for prekey_id={prekey_id}")
        return

    # Parse JSON
    event_data = crypto.parse_json(blob)
    owner_peer_id = event_data['created_by']

    # Insert into transit_prekeys table with owner (device-wide)
    unsafedb.execute(
        "INSERT OR IGNORE INTO transit_prekeys (prekey_id, owner_peer_id, public_key, private_key, created_at) VALUES (?, ?, ?, ?, ?)",
        (prekey_id, owner_peer_id, crypto.b64decode(event_data['public_key']),
         crypto.b64decode(event_data['private_key']), event_data['created_at'])
    )

    # Mark as valid for this peer
    log.warning(f"[VALID_EVENT] Marking transit_prekey {prekey_id[:20]}... as valid for peer {recorded_by[:20]}...")

    # Check if blob is in store before marking as valid
    in_store = unsafedb.query_one("SELECT 1 FROM store WHERE id = ?", (prekey_id,))
    if not in_store:
        log.error(f"[VALID_EVENT_BUG] ❌ Marking transit_prekey {prekey_id[:20]}... as valid but blob NOT in store!")

    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (prekey_id, recorded_by)
    )


def get_transit_prekey_for_peer(peer_shared_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the transit pre-key for a specific peer in format expected by crypto.wrap().

    Prekeys are public and meant to be shared for encryption, indexed by peer_shared_id
    (public identity) rather than local peer_id.

    Args:
        peer_shared_id: Peer's peer_shared_id (public identity) to get prekey for
        recorded_by: Local peer_id requesting access (for subjective view)
        db: Database connection

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'type': 'asymmetric'}
        or None if prekey not found
    """
    import logging
    lookup_log = logging.getLogger(__name__)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    lookup_log.info(f"get_transit_prekey_for_peer() looking for prekey of peer_shared_id={peer_shared_id[:20]}..., requested_by={recorded_by[:20]}...")

    # Debug: show all transit_prekeys_shared for this recorded_by
    all_shared = safedb.query("SELECT peer_id, transit_prekey_id, transit_prekey_shared_id FROM transit_prekeys_shared WHERE recorded_by = ?", (recorded_by,))
    lookup_log.info(f"get_transit_prekey_for_peer() ALL transit_prekeys_shared for {recorded_by[:20]}: {[(r['peer_id'][:20], r['transit_prekey_id'][:20] if r['transit_prekey_id'] else 'NULL', r['transit_prekey_shared_id'][:20]) for r in all_shared[:5]]}")

    result = safedb.query_one(
        "SELECT transit_prekey_shared_id, transit_prekey_id, public_key FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ? ORDER BY created_at DESC, transit_prekey_shared_id DESC LIMIT 1",
        (peer_shared_id, recorded_by)
    )

    if not result:
        lookup_log.warning(f"get_transit_prekey_for_peer() NO PREKEY FOUND for peer_shared_id={peer_shared_id[:20]}...")
        return None

    # Use transit_prekey_shared_id as the hint/id for asymmetric keys (event ID in blob store)
    transit_prekey_shared_id_bytes = crypto.b64decode(result['transit_prekey_shared_id'])
    lookup_log.info(f"get_transit_prekey_for_peer() FOUND transit_prekey_shared_id={result['transit_prekey_shared_id'][:20]}... for peer_shared_id={peer_shared_id[:20]}...")

    return {
        'id': transit_prekey_shared_id_bytes,
        'public_key': result['public_key'],
        'type': 'asymmetric'
    }
