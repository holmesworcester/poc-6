"""Framework utilities for unified update event pattern.

Provides:
- Global counter (Lamport clock) management per peer
- Window function-based winner selection for LWW (Last-Writer-Wins)
"""
from typing import Any, Union
import logging

log = logging.getLogger(__name__)


def get_next_global_count(peer_id: str, db: Any) -> int:
    """Get next global count for this peer and persist the increment.

    Implements Lamport clock: each peer maintains its own counter that increments
    with every event created locally, ensuring globally comparable timestamps.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        int: Next global count value for this peer
    """
    from db import create_unsafe_db

    unsafedb = create_unsafe_db(db)

    # Query current highest count for this peer
    row = unsafedb.query_one(
        "SELECT highest_count FROM peer_global_counter WHERE peer_id = ?",
        (peer_id,)
    )
    current = row['highest_count'] if row else 0
    next_gc = current + 1

    # Persist the increment
    unsafedb.execute(
        "INSERT OR REPLACE INTO peer_global_counter (peer_id, highest_count) VALUES (?, ?)",
        (peer_id, next_gc)
    )
    db.commit()

    log.debug(f"get_next_global_count({peer_id[:8]}...) returned {next_gc}")
    return next_gc


def update_highest_count_seen(peer_id: str, gc: int, db: Any) -> None:
    """Update highest global count seen from a peer during sync.

    When receiving events from sync, we update our tracking to ensure we never
    reuse global count values even after restart. This maintains the Lamport clock invariant.

    Args:
        peer_id: Peer ID that created the event
        gc: Global count value from the received event
        db: Database connection
    """
    from db import create_unsafe_db

    unsafedb = create_unsafe_db(db)

    # Query current highest count for this peer
    row = unsafedb.query_one(
        "SELECT highest_count FROM peer_global_counter WHERE peer_id = ?",
        (peer_id,)
    )
    current = row['highest_count'] if row else 0

    # Only update if we've seen a higher count
    if gc > current:
        unsafedb.execute(
            "INSERT OR REPLACE INTO peer_global_counter (peer_id, highest_count) VALUES (?, ?)",
            (peer_id, gc)
        )
        db.commit()
        log.debug(f"update_highest_count_seen({peer_id[:8]}..., {gc}) updated to {gc}")
    else:
        log.debug(f"update_highest_count_seen({peer_id[:8]}..., {gc}) no update needed (current={current})")


def get_winners(
    table: str,
    partition_field: Union[str, list],
    filters: dict,
    db: Any,
    id_field: str = 'event_id'
) -> list:
    """Get winning events for each partition using window function (LWW).

    Implements Last-Writer-Wins via SQL window function:
    - Ranks events by global_count DESC (highest wins)
    - Ties broken by id_field DESC (lexicographic, deterministic)
    - Returns one winner per partition

    Args:
        table: Table name (e.g., 'user_names', 'message_reactions')
        partition_field: Field(s) to partition by (str or list of strings)
                        E.g., 'user_id' or ['message_id', 'emoji']
        filters: Dict of WHERE clause conditions
                E.g., {'recorded_by': peer_id, 'user_id': [u1, u2]}
        db: Database connection
        id_field: Primary event ID column name (default: 'event_id')
                 E.g., 'update_id' for message_updates, 'reaction_id' for message_reactions

    Returns:
        list: Rows with rn=1 (winners for each partition)

    Example:
        # Get winning username for user_id
        winners = get_winners('user_names', 'user_id',
                             {'recorded_by': peer_id, 'user_id': [user_id]}, db)

        # Get winning reaction for each emoji in message
        winners = get_winners('message_reactions', ['message_id', 'emoji'],
                             {'recorded_by': peer_id, 'message_id': [msg_id]}, db)

        # Get winning message update
        winners = get_winners('message_updates', 'message_id',
                             {'recorded_by': peer_id, 'message_id': [msg_id]}, db,
                             id_field='update_id')
    """
    from db import create_unsafe_db, create_safe_db, SUBJECTIVE_TABLES

    # Use SafeDB for subjective tables, UnsafeDB for device tables
    is_subjective = table in SUBJECTIVE_TABLES
    if is_subjective:
        # For subjective tables, recorded_by must be in filters
        if 'recorded_by' not in filters:
            raise ValueError(f"Table '{table}' is subjective and requires 'recorded_by' in filters")
        recorded_by = filters['recorded_by']
        dbconn = create_safe_db(db, recorded_by=recorded_by)
    else:
        dbconn = create_unsafe_db(db)

    # Build WHERE clause
    # Always include recorded_by for subjective tables in the WHERE clause because
    # SafeDB's scoping may not work properly within CTEs
    where_parts = []
    where_vals = []
    for key, val in filters.items():
        if isinstance(val, list):
            placeholders = ','.join(['?' for _ in val])
            where_parts.append(f"{key} IN ({placeholders})")
            where_vals.extend(val)
        else:
            where_parts.append(f"{key} = ?")
            where_vals.append(val)

    where_clause = " AND ".join(where_parts) if where_parts else "1=1"

    # Build partition expression
    if isinstance(partition_field, list):
        partition_expr = ", ".join(partition_field)
    else:
        partition_expr = partition_field

    # Build query with window function
    query = f"""
    WITH ranked AS (
        SELECT *,
               ROW_NUMBER() OVER (
                   PARTITION BY {partition_expr}
                   ORDER BY global_count DESC, {id_field} DESC
               ) as rn
        FROM {table}
        WHERE {where_clause}
    )
    SELECT * FROM ranked WHERE rn = 1
    """

    log.debug(f"get_winners({table}, {partition_field}, filters={list(filters.keys())})")
    return dbconn.query(query, where_vals)
