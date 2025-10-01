"""Store management functions."""
from typing import Any
from crypto import crypto
from events import seen

def store(blob: bytes, db: Any, t_ms: int, return_dupes: bool) -> str:
    id = crypto.hash(blob)
    """Write a blob to the store and return the id unless return_dupes is False and it already exists."""
    db.execute("INSERT OR IGNORE INTO store (id, blob, created_at) VALUES (?, ?, ?)", (id, blob, t_ms))
    if return_dupes:
        return id
    else:
        if db.changes() > 0: # but we might have changes from other sources-- how to isolate?
            return id
        else:
            return ""

def store_with_seen(blob: bytes, creator: str, t_ms: int, db: Any) -> str:
    """Store a blob with the corresponding seen events and return the seen_id."""
    id = store(blob, t_ms, db, return_dupes=True)
    """Create and store a seen event for the blob and return the seen_id."""
    seen_id = seen.create(id, creator, t_ms, db, return_dupes=False)
    return seen_id