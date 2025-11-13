"""Store management functions."""
from typing import Any
import logging
import crypto
from db import UnsafeDB

log = logging.getLogger(__name__)

# Batch mode flag for reducing logging during bulk operations
_batch_mode = False

def blob(blob: bytes, t_ms: int, return_dupes: bool, unsafedb: UnsafeDB) -> str:
    """Write a blob to the store and return the id unless return_dupes is False and it already exists."""
    id = crypto.hash(blob)
    id_str = crypto.b64encode(id)
    log.debug(f"store.blob() storing blob with id={id_str}, size={len(blob)}B, t_ms={t_ms}, return_dupes={return_dupes}")

    # Check if blob already exists
    existing = unsafedb.query_one("SELECT 1 FROM store WHERE id = ?", (id_str,))

    unsafedb.execute("INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)", (id_str, blob, t_ms))

    # Encode ID as base64 for more compact string representation
    if return_dupes:
        if not existing and not _batch_mode:
            log.warning(f"[STORE_BLOB] NEW blob stored: id={id_str[:20]}..., size={len(blob)}B, return_dupes=True")
        elif not _batch_mode:
            log.debug(f"store.blob() duplicate blob (already exists): id={id_str[:20]}..., return_dupes=True")
        return id_str
    else:
        if not existing:
            if not _batch_mode:
                log.warning(f"[STORE_BLOB] NEW blob stored: id={id_str[:20]}..., size={len(blob)}B")
            return id_str
        else:
            if not _batch_mode:
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
    from events.transit import recorded
    from db import create_unsafe_db
    if not _batch_mode:
        log.info(f"store.event() called: recorded_by={recorded_by}, t_ms={t_ms}")

    unsafedb = create_unsafe_db(db)
    event_id = blob(event_blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    if not _batch_mode:
        log.debug(f"store.event() blob stored with event_id={event_id}")

    # Create and store a recorded event for the blob
    # Note: recorded.create() and recorded.project() will create their own safe/unsafe as needed
    recorded_id = recorded.create(event_id, recorded_by, t_ms, db, return_dupes=False)
    if not _batch_mode:
        log.debug(f"store.event() recorded event created: recorded_id={recorded_id}")

    # Project the recorded event (which will dispatch to the referenced event's projector)
    recorded.project(recorded_id, db)
    if not _batch_mode:
        log.info(f"store.event() completed projection for event_id={event_id}")

    return event_id


def get(blob_id: str, unsafedb: UnsafeDB) -> bytes:
    """Get a blob from the store by its ID (base64 encoded string)."""
    # Query directly with base64 string (store now uses TEXT for id)
    row = unsafedb.query_one("SELECT blob FROM store WHERE id = ?", (blob_id,))
    if row:
        log.debug(f"store.get() found blob: id={blob_id}, size={len(row['blob'])}B")
        return row['blob']
    else:
        log.warning(f"store.get() blob not found: id={blob_id}")
        return b''


def batch_store_events(event_blobs: list[bytes], recorded_by: str, t_ms: int, db: Any) -> list[str]:
    """Store multiple events without immediate projection (for bulk slice creation).

    This is a performance optimization for operations like file slice batch creation
    where projection can be deferred. Events are stored and recorded entries created,
    but projection is skipped.

    Args:
        event_blobs: List of event blobs to store
        recorded_by: The peer_id recording these events
        t_ms: Timestamp in milliseconds
        db: Database connection

    Returns:
        List of event IDs corresponding to the input blobs
    """
    from events.transit import recorded
    from db import create_unsafe_db

    unsafedb = create_unsafe_db(db)
    event_ids = []

    for event_blob in event_blobs:
        # Store blob
        event_id = blob(event_blob, t_ms, return_dupes=True, unsafedb=unsafedb)
        event_ids.append(event_id)

        # Create recorded entry WITHOUT projection
        # This allows the event to be stored and tracked, but defers expensive projection
        recorded.create(event_id, recorded_by, t_ms, db, return_dupes=False)

    if not _batch_mode:
        log.info(f"store.batch_store_events() stored {len(event_ids)} events without projection")
    else:
        log.debug(f"store.batch_store_events() stored {len(event_ids)} events without projection")

    return event_ids