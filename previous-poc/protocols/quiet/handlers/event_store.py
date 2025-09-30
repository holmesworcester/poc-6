"""
Event Store handler - Stores events and manages purging of invalid events.

From plan.md:
- Filter: `write_to_store: true`
- Action: Stores event data in database, including network metadata
- Purge Function: Marks invalid events as purged while keeping event_id for duplicate detection
"""

# Removed core.types import
import sqlite3
import time
from typing import Dict, List, Optional, Any
from core.handlers import Handler
import json


def filter_func(envelope: dict[str, Any]) -> bool:
    """
    Process envelopes that need to be stored.
    """
    if envelope.get('stored') is True:
        return False
    # Store events requested by upstream when we have an id. Prefer encrypted blobs,
    # plaintext fallback only when explicitly requested by upstream (tests).
    if envelope.get('write_to_store') is True and envelope.get('event_id'):
        return True
    return False


def handler(envelope: dict[str, Any], db: sqlite3.Connection) -> dict[str, Any]:
    """
    Store event data in database.
    
    Args:
        envelope: dict[str, Any] with write_to_store flag
        db: Database connection
        
    Returns:
        dict[str, Any] with stored: true
    """
    event_id = envelope.get('event_id')
    if not event_id:
        envelope['error'] = "No event_id to store"
        return envelope

    # Local-only events are generated locally by the pipeline only.
    # Do not attempt to infer or gate here; upstream handlers enforce this.
    
    # Check if already stored
    cursor = db.execute(
        "SELECT 1 FROM events WHERE event_id = ?",
        (event_id,),
    )
    existing = cursor.fetchone()
    if existing:
        envelope['stored'] = True
        return envelope
    
    # Store event
    try:
        # Prefer event_blob if provided; else build minimal event_blob
        event_blob: bytes | None = None
        # Allow handlers to set event_blob explicitly (e.g., crypto with header+hint)
        if isinstance(envelope.get('event_blob'), (bytes, bytearray)):
            event_blob = envelope['event_blob']  # type: ignore[index]
        elif isinstance(envelope.get('event_plaintext'), dict):
            try:
                event_blob = json.dumps(envelope['event_plaintext'], sort_keys=True, separators=(',', ':')).encode('utf-8')  # type: ignore[index]
            except Exception:
                event_blob = None

        visibility = 'network' if not envelope.get('local_only') else 'local-only'

        db.execute(
            """
            INSERT INTO events (event_id, event_blob, visibility)
            VALUES (?, ?, ?)
            """,
            (
                event_id,
                event_blob or b"",
                visibility,
            ),
        )
        
        db.commit()
        print(f"[event_store] Stored event {event_id} ({envelope.get('event_type')})")
        envelope['stored'] = True

        # Indexing of validated IDs and unblocking is owned by resolve_deps.

        # Record visibility for the receiving/sending identity so queries
        # can scope results to what each identity has actually seen.
        try:
            # Trust upstream to set to_peer to the receiving peer_secret_id.
            viewer_id = envelope.get('to_peer')
            if isinstance(viewer_id, str) and viewer_id:
                first_seen_ms = int(time.time() * 1000)
                db.execute(
                    """
                    INSERT OR IGNORE INTO seen_events (identity_id, event_id, seen_at)
                    VALUES (?, ?, ?)
                    """,
                    (viewer_id, event_id, first_seen_ms),
                )
                db.commit()
                print(f"[event_store] Seen: peer_secret {viewer_id} now has {event_id}")
        except Exception:
            # Best-effort; do not fail storing due to visibility tracking
            pass
        
    except Exception as e:
        db.rollback()
        envelope['error'] = f"Failed to store event: {str(e)}"

    # Mark as stored to prevent re-processing
    envelope['stored'] = True

    return envelope



def purge_event(event_id: str, db: sqlite3.Connection, reason: str = "validation_failed") -> bool:
    """
    No-op purge placeholder. With a minimal ES, invalid events are not stored.
    Keep function for compatibility; remove when callers are updated.
    """
    try:
        # Best-effort: delete from projections if present
        db.execute("DELETE FROM projected_events WHERE event_id = ?", (event_id,))
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False

class EventStoreHandler(Handler):
    """Handler for event store."""

    @property
    def name(self) -> str:
        return "event_store"

    def filter(self, envelope: dict[str, Any]) -> bool:
        """Check if this handler should process the envelope."""
        return filter_func(envelope)

    def process(self, envelope: dict[str, Any], db: sqlite3.Connection) -> List[dict[str, Any]]:
        """Process the envelope."""
        result = handler(envelope, db)
        if result:
            return [result]
        return []
