"""Generic cascading deletion for valid_events.

When an event is deleted, this module ensures all dependent events
are also removed from valid_events to maintain convergence.

Convergence property: regardless of event ordering, the final valid_events
table is the same. This requires that if event B depends on event A,
deleting A must also delete B from valid_events.
"""
import logging
from db import SafeDB

log = logging.getLogger(__name__)


def cascade_delete_from_valid_events(
    event_id: str,
    recorded_by: str,
    safedb: SafeDB,
    _visited: set = None
) -> int:
    """Recursively delete event and all dependents from valid_events.

    Args:
        event_id: The event being deleted
        recorded_by: Peer scope (SafeDB is already scoped to this peer)
        safedb: SafeDB instance
        _visited: Internal cycle detection set (prevents infinite recursion)

    Returns:
        Total number of events deleted from valid_events (including recursively deleted dependents)
    """
    if _visited is None:
        _visited = set()

    # Cycle detection
    if event_id in _visited:
        return 0

    _visited.add(event_id)
    deleted_count = 0

    # Find all children (events that depend on this one)
    children = safedb.query(
        """SELECT DISTINCT child_event_id
           FROM event_dependencies
           WHERE parent_event_id = ? AND recorded_by = ?""",
        (event_id, recorded_by)
    )

    # Recursively delete children first (depth-first traversal)
    for child in children:
        deleted_count += cascade_delete_from_valid_events(
            child['child_event_id'],
            recorded_by,
            safedb,
            _visited
        )

    # Delete this event from valid_events
    safedb.execute(
        "DELETE FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (event_id, recorded_by)
    )

    log.debug(f"cascade_delete_from_valid_events() deleted {event_id[:20]}... and {deleted_count} dependents for peer {recorded_by[:20]}...")

    return deleted_count + 1
