"""Peer event type (local-only identity keypair)."""
from typing import Any
import json
import crypto
import store


def create(t_ms: int, db: Any) -> tuple[str, str]:
    """Create a peer (local-only keypair) and its shareable peer_shared event.

    Returns (peer_id, peer_shared_id): peer_id is local (for signing), peer_shared_id is public (for created_by).
    """
    from events import peer_shared

    # Generate keypair
    private_key, public_key = crypto.generate_keypair()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'peer',
        'public_key': crypto.b64encode(public_key),
        'private_key': crypto.b64encode(private_key),
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # First store the blob to get the peer_id
    peer_id = store.blob(blob, t_ms, return_dupes=True, db=db)

    # Then create first_seen wrapper where peer sees itself
    from events import first_seen
    first_seen_id = first_seen.create(peer_id, peer_id, t_ms, db, return_dupes=False)
    first_seen.project(first_seen_id, db)

    # Create shareable peer_shared event
    peer_shared_id = peer_shared.create(peer_id, t_ms, db)

    return (peer_id, peer_shared_id)


def project(peer_id: str, seen_by_peer_id: str, db: Any) -> None:
    """Project peer event into peers table (for local peers, both IDs are the same)."""
    # Get blob from store
    blob = store.get(peer_id, db)
    if not blob:
        return

    # Parse JSON
    event_data = json.loads(blob.decode())

    # Insert into peers table (local-only, not shareable)
    db.execute(
        """INSERT OR IGNORE INTO peers (peer_id, public_key, private_key, created_at)
           VALUES (?, ?, ?, ?)""",
        (
            peer_id,
            event_data['public_key'],
            crypto.b64decode(event_data['private_key']),
            event_data['created_at']
        )
    )

    # Mark as valid for this peer
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, seen_by_peer_id) VALUES (?, ?)",
        (peer_id, seen_by_peer_id)
    )


def get_private_key(peer_id: str, db: Any) -> bytes:
    """Get private key for a peer_id."""
    row = db.query_one("SELECT private_key FROM peers WHERE peer_id = ?", (peer_id,))
    if not row:
        raise ValueError(f"peer not found: {peer_id}")
    return row['private_key']


def get_public_key(peer_id: str, db: Any) -> bytes:
    """Get public key for a peer_id from peers table."""
    row = db.query_one("SELECT public_key FROM peers WHERE peer_id = ?", (peer_id,))
    if not row:
        raise ValueError(f"peer not found: {peer_id}")
    # public_key is stored as base64 string in the table
    return crypto.b64decode(row['public_key'])
