"""Peer event type (local-only identity keypair)."""
from typing import Any
import json
import crypto
import store


def create(t_ms: int, db: Any) -> str:
    """Create a peer (local-only keypair), store and project it, return peer_id."""
    # Generate keypair
    private_key, public_key = crypto.generate_keypair()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'peer',
        'public_key': public_key.hex(),
        'private_key': private_key.hex(),
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store directly (no first_seen for local-only events)
    peer_id = store.store(blob, t_ms, return_dupes=True, db=db)

    # Project immediately
    project(peer_id, db)

    return peer_id


def project(peer_id: str, db: Any) -> None:
    """Project peer event into peers table."""
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
            bytes.fromhex(event_data['private_key']),
            event_data['created_at']
        )
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
    # public_key is stored as hex string in the table
    return bytes.fromhex(row['public_key'])
