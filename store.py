"""Store management functions."""
from typing import Any
import base64
import crypto

def store(blob: bytes, t_ms: int, return_dupes: bool, db: Any) -> str:
    id = crypto.hash(blob)
    """Write a blob to the store and return the id unless return_dupes is False and it already exists."""
    db.execute("INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)", (id, blob, t_ms))
    # Encode ID as base64 for more compact string representation
    id_str = base64.b64encode(id).decode('ascii')
    if return_dupes:
        return id_str
    else:
        if db.changes() > 0: # but we might have changes from other sources-- how to isolate?
            return id_str
        else:
            return ""

def store_with_first_seen(blob: bytes, seen_by_peer_secret_id: str, t_ms: int, db: Any) -> str:
    """Store a blob and create a first_seen event. For local events, seen_by=creating peer. For incoming, seen_by=receiving peer (from transit_key)."""
    from events import first_seen
    event_id = store(blob, t_ms, return_dupes=True, db=db)
    # Create and store a first_seen event for the blob
    first_seen_id = first_seen.create(event_id, seen_by_peer_secret_id, t_ms, db, return_dupes=False)
    # Project the first_seen event (which will dispatch to the referenced event's projector)
    first_seen.project(first_seen_id, db)
    return event_id


def get(blob_id: str, db: Any) -> bytes:
    """Get a blob from the store by its ID (base64 encoded string)."""
    # Decode base64 ID to bytes for database lookup
    id_bytes = base64.b64decode(blob_id)
    row = db.query_one("SELECT blob FROM store WHERE id = ?", (id_bytes,))
    if row:
        return row['blob']
    return b''