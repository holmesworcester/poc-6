"""Store management functions."""
from typing import Any
import crypto

def store(blob: bytes, db: Any, t_ms: int, return_dupes: bool) -> str:
    id = crypto.hash(blob)
    """Write a blob to the store and return the id unless return_dupes is False and it already exists."""
    db.execute("INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)", (id, blob, t_ms))
    if return_dupes:
        return id.hex()
    else:
        if db.changes() > 0: # but we might have changes from other sources-- how to isolate?
            return id.hex()
        else:
            return ""

def store_with_first_seen(blob: bytes, creator: str, t_ms: int, db: Any) -> str:
    """Store a blob and create a first_seen event. For local events, creator=creating peer. For incoming, creator=receiving peer (from transit_key)."""
    from events import first_seen
    id = store(blob, db, t_ms, return_dupes=True)
    """Create and store a first_seen event for the blob and return the first_seen_id."""
    first_seen_id = first_seen.create(id, creator, t_ms, db, return_dupes=False)
    return first_seen_id


def get(blob_id: str, db: Any) -> bytes:
    """Get a blob from the store by its ID."""
    row = db.query_one("SELECT blob FROM store WHERE id = ?", (bytes.fromhex(blob_id),))
    if row:
        return row['blob']
    return b''