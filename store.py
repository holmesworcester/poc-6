"""Store management functions."""
from typing import Any
import logging
import crypto

log = logging.getLogger(__name__)

def blob(blob: bytes, t_ms: int, return_dupes: bool, db: Any) -> str:
    """Write a blob to the store and return the id unless return_dupes is False and it already exists."""
    id = crypto.hash(blob)
    id_str = crypto.b64encode(id)
    log.debug(f"store.blob() storing blob with id={id_str}, size={len(blob)}B, t_ms={t_ms}, return_dupes={return_dupes}")

    db.execute("INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)", (id, blob, t_ms))

    # Encode ID as base64 for more compact string representation
    if return_dupes:
        log.debug(f"store.blob() returning id={id_str} (return_dupes=True)")
        return id_str
    else:
        if db.changes() > 0: # but we might have changes from other sources-- how to isolate?
            log.info(f"store.blob() new blob stored: id={id_str}")
            return id_str
        else:
            log.debug(f"store.blob() duplicate blob skipped: id={id_str}")
            return ""

def event(event_blob: bytes, recorded_by: str, t_ms: int, db: Any) -> str:
    """Store an event with recorded wrapper and project it.

    For local events, recorded_by=creating peer. For incoming, recorded_by=receiving peer.
    """
    from events import recorded
    log.info(f"store.event() called: recorded_by={recorded_by}, t_ms={t_ms}")

    event_id = blob(event_blob, t_ms, return_dupes=True, db=db)
    log.debug(f"store.event() blob stored with event_id={event_id}")

    # Create and store a recorded event for the blob
    recorded_id = recorded.create(event_id, recorded_by, t_ms, db, return_dupes=False)
    log.debug(f"store.event() recorded event created: recorded_id={recorded_id}")

    # Project the recorded event (which will dispatch to the referenced event's projector)
    recorded.project(recorded_id, db)
    log.info(f"store.event() completed projection for event_id={event_id}")

    return event_id


def get(blob_id: str, db: Any) -> bytes:
    """Get a blob from the store by its ID (base64 encoded string)."""
    # Decode base64 ID to bytes for database lookup
    id_bytes = crypto.b64decode(blob_id)
    row = db.query_one("SELECT blob FROM store WHERE id = ?", (id_bytes,))
    if row:
        log.debug(f"store.get() found blob: id={blob_id}, size={len(row['blob'])}B")
        return row['blob']
    else:
        log.warning(f"store.get() blob not found: id={blob_id}")
        return b''