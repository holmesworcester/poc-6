"""Key event type (local-only symmetric encryption key)."""
from typing import Any
import json
import crypto
import store

ID_SIZE = 16  # bytes (128 bits) - BLAKE2b hash size


def create(t_ms: int, db: Any) -> str:
    """Create a key (local-only symmetric key), store and project it, return key_id."""
    # Generate symmetric key
    key = crypto.generate_secret()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'key',
        'key': key.hex(),
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store directly (no first_seen for local-only events)
    key_id = store.store(blob, t_ms, return_dupes=True, db=db)

    # Project immediately
    project(key_id, db)

    return key_id


def project(key_id: str, db: Any) -> None:
    """Project key event into keys table."""
    # Get blob from store
    blob = store.get(key_id, db)
    if not blob:
        return

    # Parse JSON
    event_data = json.loads(blob.decode())

    # Insert into keys table (local-only, not shareable)
    db.execute(
        """INSERT OR IGNORE INTO keys (key_id, key, created_at)
           VALUES (?, ?, ?)""",
        (
            key_id,
            bytes.fromhex(event_data['key']),
            event_data['created_at']
        )
    )


def extract_id(blob: bytes) -> bytes:
    """Extract the first ID_SIZE bytes from a wrapped blob."""
    return blob[:ID_SIZE]


def get_key_by_id(id_bytes: bytes, db: Any) -> dict[str, Any] | None:
    """Get key from database by id bytes. Returns None if not found."""
    import base64
    key_id = base64.b64encode(id_bytes).decode('ascii')
    row = db.query_one("SELECT key FROM keys WHERE key_id = ?", (key_id,))
    if not row:
        return None

    return {
        'id': id_bytes,  # Use the provided id bytes directly as blob prefix
        'key': row['key'],  # Already bytes from DB
        'type': 'symmetric'
    }


def get_key(key_id: str, db: Any) -> dict[str, Any]:
    """Get key from database in format expected by crypto.wrap()."""
    import base64
    row = db.query_one("SELECT key FROM keys WHERE key_id = ?", (key_id,))
    if not row:
        raise ValueError(f"key not found: {key_id}")

    return {
        'id': base64.b64decode(key_id),  # Decode base64 key_id to bytes for use as blob prefix
        'key': row['key'],  # Already bytes from DB
        'type': 'symmetric'
    }
