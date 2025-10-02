"""Sync state tracking for window-based sync."""
from typing import Any
from . import windows, constants


def get_sync_state(from_peer_id: str, to_peer_id: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Get sync state for a peer pair.

    Args:
        from_peer_id: Local peer doing the syncing
        to_peer_id: Remote peer being synced with
        t_ms: Current timestamp (for initialization)
        db: Database connection

    Returns:
        Sync state dict with keys: last_window, w_param, total_events_seen
    """
    row = db.query_one(
        "SELECT last_window, w_param, total_events_seen FROM sync_state WHERE from_peer_id = ? AND to_peer_id = ?",
        (from_peer_id, to_peer_id)
    )

    if row:
        return {
            'last_window': row['last_window'],
            'w_param': row['w_param'],
            'total_events_seen': row['total_events_seen']
        }

    # Initialize new sync state
    return {
        'last_window': -1,  # Not started
        'w_param': constants.DEFAULT_W,
        'total_events_seen': 0
    }


def update_sync_state(
    from_peer_id: str,
    to_peer_id: str,
    last_window: int,
    w_param: int,
    total_events_seen: int,
    t_ms: int,
    db: Any
) -> None:
    """Update sync state for a peer pair.

    Args:
        from_peer_id: Local peer doing the syncing
        to_peer_id: Remote peer being synced with
        last_window: Last window that was synced
        w_param: Current window parameter
        total_events_seen: Total events seen
        t_ms: Current timestamp
        db: Database connection
    """
    db.execute(
        """INSERT INTO sync_state (from_peer_id, to_peer_id, last_window, w_param, total_events_seen, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (from_peer_id, to_peer_id)
           DO UPDATE SET
               last_window = excluded.last_window,
               w_param = excluded.w_param,
               total_events_seen = excluded.total_events_seen,
               updated_at = excluded.updated_at""",
        (from_peer_id, to_peer_id, last_window, w_param, total_events_seen, t_ms)
    )


def get_next_window(from_peer_id: str, to_peer_id: str, t_ms: int, db: Any) -> tuple[int, int]:
    """Get the next window to sync for a peer pair.

    Args:
        from_peer_id: Local peer doing the syncing
        to_peer_id: Remote peer being synced with
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Tuple of (window_id, w_param)
    """
    state = get_sync_state(from_peer_id, to_peer_id, t_ms, db)

    # Get next window (wraps around)
    total_windows = windows.compute_window_count(state['w_param'])
    next_window = (state['last_window'] + 1) % total_windows

    return next_window, state['w_param']


def mark_window_synced(
    from_peer_id: str,
    to_peer_id: str,
    window_id: int,
    t_ms: int,
    db: Any
) -> None:
    """Mark a window as synced and update state.

    Args:
        from_peer_id: Local peer doing the syncing
        to_peer_id: Remote peer being synced with
        window_id: Window that was just synced
        t_ms: Current timestamp
        db: Database connection
    """
    state = get_sync_state(from_peer_id, to_peer_id, t_ms, db)

    # Update last window
    state['last_window'] = window_id

    # Check if we should increase w parameter
    # (Dynamic adjustment: when events > W × 450, increase w)
    current_w = state['w_param']
    current_windows = windows.compute_window_count(current_w)
    threshold = current_windows * constants.EVENTS_PER_WINDOW_TARGET

    if state['total_events_seen'] > threshold:
        # Increase w by 1
        state['w_param'] = current_w + 1

    update_sync_state(
        from_peer_id,
        to_peer_id,
        state['last_window'],
        state['w_param'],
        state['total_events_seen'],
        t_ms,
        db
    )
