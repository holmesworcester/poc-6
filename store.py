"""Store management functions."""
from typing import Any
import logging
import crypto
from db import UnsafeDB

log = logging.getLogger(__name__)

def blob(blob: bytes, t_ms: int, return_dupes: bool, unsafedb: UnsafeDB) -> str:
    """Write a blob to the store and return the id unless return_dupes is False and it already exists."""
    id = crypto.hash(blob)
    id_str = crypto.b64encode(id)
    log.debug(f"store.blob() storing blob with id={id_str}, size={len(blob)}B, t_ms={t_ms}, return_dupes={return_dupes}")

    unsafedb.execute("INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)", (id, blob, t_ms))

    # Encode ID as base64 for more compact string representation
    if return_dupes:
        log.debug(f"store.blob() returning id={id_str} (return_dupes=True)")
        return id_str
    else:
        if unsafedb.changes() > 0:
            log.info(f"store.blob() new blob stored: id={id_str}")
            return id_str
        else:
            log.debug(f"store.blob() duplicate blob skipped: id={id_str}")
            return ""

def event(event_blob: bytes, recorded_by: str, t_ms: int, db: Any) -> str:
    """Store an event with recorded wrapper and project it.

    For local events, recorded_by=creating peer. For incoming, recorded_by=receiving peer.

    Args:
        event_blob: The event blob to store
        recorded_by: The peer_id recording this event
        t_ms: Timestamp in milliseconds
        db: Raw Database instance (creates safe/unsafe internally)
    """
    from events import recorded
    from db import create_unsafe_db
    log.info(f"store.event() called: recorded_by={recorded_by}, t_ms={t_ms}")

    unsafedb = create_unsafe_db(db)
    event_id = blob(event_blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    log.debug(f"store.event() blob stored with event_id={event_id}")

    # Create and store a recorded event for the blob
    # Note: recorded.create() and recorded.project() will create their own safe/unsafe as needed
    recorded_id = recorded.create(event_id, recorded_by, t_ms, db, return_dupes=False)
    log.debug(f"store.event() recorded event created: recorded_id={recorded_id}")

    # Project the recorded event (which will dispatch to the referenced event's projector)
    recorded.project(recorded_id, db)
    log.info(f"store.event() completed projection for event_id={event_id}")

    return event_id


def get(blob_id: str, unsafedb: UnsafeDB) -> bytes:
    """Get a blob from the store by its ID (base64 encoded string)."""
    # Decode base64 ID to bytes for database lookup
    id_bytes = crypto.b64decode(blob_id)
    row = unsafedb.query_one("SELECT blob FROM store WHERE id = ?", (id_bytes,))
    if row:
        log.debug(f"store.get() found blob: id={blob_id}, size={len(row['blob'])}B")
        return row['blob']
    else:
        log.warning(f"store.get() blob not found: id={blob_id}")
        return b''