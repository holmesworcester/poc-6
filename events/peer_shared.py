"""Peer shared event type (shareable public identity)."""
from typing import Any
import json
import crypto
import store
from events import peer


def create(peer_id: str, t_ms: int, db: Any) -> str:
    """Create a shareable peer_shared event from a local peer."""
    # Get public key from local peer
    public_key = peer.get_public_key(peer_id, db)

    # Create event dict (plaintext for now, could be signed later)
    event_data = {
        'type': 'peer_shared',
        'public_key': crypto.b64encode(public_key),
        'peer_id': peer_id,  # Link back to local peer
        'created_at': t_ms
    }

    # For now, store as plaintext (could encrypt/sign later)
    blob = json.dumps(event_data).encode()

    # Store event with first_seen wrapper and projection
    peer_shared_id = store.event(blob, peer_id, t_ms, db)

    return peer_shared_id


def project(peer_shared_id: str, seen_by_peer_id: str, received_at: int, db: Any) -> str | None:
    """Project peer_shared event into peers_shared and shareable_events tables."""
    # Get blob from store
    blob = store.get(peer_shared_id, db)
    if not blob:
        return None

    # Parse JSON (plaintext for now)
    event_data = json.loads(blob.decode())

    # Insert into peers_shared table
    db.execute(
        """INSERT OR IGNORE INTO peers_shared
           (peer_shared_id, public_key, created_at, seen_by_peer_id, received_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            peer_shared_id,
            event_data['public_key'],
            event_data['created_at'],
            seen_by_peer_id,
            received_at
        )
    )

    # Insert into shareable_events
    db.execute(
        """INSERT OR IGNORE INTO shareable_events (event_id, peer_id, created_at)
           VALUES (?, ?, ?)""",
        (
            peer_shared_id,
            seen_by_peer_id,  # The peer who created/shared this
            event_data['created_at']
        )
    )

    # Mark as valid for this peer
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, seen_by_peer_id) VALUES (?, ?)",
        (peer_shared_id, seen_by_peer_id)
    )

    return peer_shared_id


def get_public_key(peer_shared_id: str, seen_by_peer_id: str, db: Any) -> bytes:
    """Get public key for a peer_shared_id from the perspective of seen_by_peer_id."""
    row = db.query_one(
        "SELECT public_key FROM peers_shared WHERE peer_shared_id = ? AND seen_by_peer_id = ? LIMIT 1",
        (peer_shared_id, seen_by_peer_id)
    )
    if not row:
        raise ValueError(f"peer_shared not found: {peer_shared_id} for peer {seen_by_peer_id}")
    # public_key is stored as base64 string
    return crypto.b64decode(row['public_key'])


def get_peer_id_for_signing(peer_shared_id: str, db: Any) -> str:
    """Get the local peer_id associated with a peer_shared_id for signing."""
    # Get the event blob
    blob = store.get(peer_shared_id, db)
    if not blob:
        raise ValueError(f"peer_shared not found: {peer_shared_id}")

    event_data = json.loads(blob.decode())
    peer_id = event_data.get('peer_id')
    if not peer_id:
        raise ValueError(f"peer_id not found in peer_shared event: {peer_shared_id}")

    return peer_id
