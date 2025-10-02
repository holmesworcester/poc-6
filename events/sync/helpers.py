"""Helper functions for sync-related operations."""
from typing import Any
from . import windows, constants
import crypto


def compute_storage_window_id(event_id_bytes: bytes) -> int:
    """Compute window ID for storage in shareable_events table.

    Uses a high w parameter (20) to support large event counts without
    needing to recompute window_ids as the database grows.

    Args:
        event_id_bytes: 16-byte event ID

    Returns:
        Window ID at w=20
    """
    return windows.compute_window_id(event_id_bytes, constants.STORAGE_W)


def query_window_id_for_w(storage_window_id: int, query_w: int) -> int:
    """Convert stored window_id (at w=20) to query window_id (at query_w).

    Args:
        storage_window_id: Window ID stored in database (at w=20)
        query_w: Target w parameter for query

    Returns:
        Window ID at query_w
    """
    if query_w >= constants.STORAGE_W:
        # Query w is higher than storage w - not supported
        raise ValueError(f"Query w={query_w} exceeds storage w={constants.STORAGE_W}")

    # Right shift to get fewer bits
    shift = constants.STORAGE_W - query_w
    return storage_window_id >> shift


def add_shareable_event(event_id: str, peer_id: str, created_at: int, db: Any) -> None:
    """Add an event to shareable_events table with computed window_id.

    Args:
        event_id: Base64-encoded event ID
        peer_id: Peer who created this event
        created_at: Event creation timestamp (ms)
        db: Database connection
    """
    # Decode event_id to get bytes for window computation
    event_id_bytes = crypto.b64decode(event_id)

    # Compute window_id for storage
    window_id = compute_storage_window_id(event_id_bytes)

    # Insert with window_id
    db.execute(
        """INSERT OR IGNORE INTO shareable_events (event_id, peer_id, created_at, window_id)
           VALUES (?, ?, ?, ?)""",
        (event_id, peer_id, created_at, window_id)
    )
