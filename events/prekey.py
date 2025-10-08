"""Prekey event type (local-only prekey keypair)."""
from typing import Any
import json
import logging
import crypto
import store
from db import create_unsafe_db, create_safe_db

log = logging.getLogger(__name__)


def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a local-only prekey event (like peer.create).

    Generates Ed25519 keypair, stores both public and private keys in event.
    Projects to local_prekeys table with owner_peer_id.

    Args:
        peer_id: Local peer ID (owner of this prekey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (prekey_id, prekey_private): The stored prekey event ID and private key bytes
    """
    log.info(f"prekey.create() creating new prekey for peer_id={peer_id}, t_ms={t_ms}")

    # Generate Ed25519 keypair for prekey
    prekey_private, prekey_public = crypto.generate_keypair()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'prekey',
        'public_key': crypto.b64encode(prekey_public),
        'private_key': crypto.b64encode(prekey_private),
        'peer_id': peer_id,
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    unsafedb = create_unsafe_db(db)

    # Store the blob to get prekey_id
    prekey_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    log.info(f"prekey.create() generated prekey_id={prekey_id}")

    # Create recorded wrapper where peer sees itself
    from events import recorded
    recorded_id = recorded.create(prekey_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    log.info(f"prekey.create() projected prekey_id={prekey_id}")
    return prekey_id, prekey_private


def project(prekey_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project prekey event into local_prekeys table with owner_peer_id."""
    log.info(f"prekey.project() prekey_id={prekey_id}, seen_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(prekey_id, unsafedb)
    if not blob:
        log.warning(f"prekey.project() blob not found for prekey_id={prekey_id}")
        return

    # Parse JSON
    event_data = crypto.parse_json(blob)
    owner_peer_id = event_data['peer_id']

    # Insert into prekeys table with owner (append-only, keep all prekeys)
    # Queries will use ORDER BY created_at DESC LIMIT 1 to get most recent
    unsafedb.execute(
        "INSERT OR IGNORE INTO prekeys (prekey_id, owner_peer_id, public_key, private_key, created_at) VALUES (?, ?, ?, ?, ?)",
        (prekey_id, owner_peer_id, crypto.b64decode(event_data['public_key']),
         crypto.b64decode(event_data['private_key']), event_data['created_at'])
    )

    # Note: prekeys_shared is populated by prekey_shared.project(), not here
    # The prekey event is local-only; the prekey_shared event is shareable

    # Mark as valid for this peer
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
    result = safedb.query_one(
        "SELECT prekey_shared_id, public_key FROM prekeys_shared WHERE peer_id = ? AND recorded_by = ? ORDER BY created_at DESC LIMIT 1",
        (peer_shared_id, recorded_by)
    )

    if not result:
        lookup_log.warning(f"get_transit_prekey_for_peer() NO PREKEY FOUND for peer_shared_id={peer_shared_id[:20]}...")
        return None

    # Use prekey_shared_id as the hint/id for asymmetric keys
    prekey_shared_id_bytes = crypto.b64decode(result['prekey_shared_id'])
    lookup_log.info(f"get_transit_prekey_for_peer() FOUND prekey_shared_id={result['prekey_shared_id']} for peer_shared_id={peer_shared_id[:20]}...")

    return {
        'id': prekey_shared_id_bytes,
        'public_key': result['public_key'],
        'type': 'asymmetric'
    }
