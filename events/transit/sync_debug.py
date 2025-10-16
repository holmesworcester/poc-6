"""Debug sync functions that run to convergence for testing."""
from typing import Any
import logging
from events.transit import sync
from db import create_safe_db

log = logging.getLogger(__name__)


def send_request_to_all_debug(t_ms: int, db: Any, max_rounds: int = 100) -> int:
    """Keep sending sync requests until convergence (simplified for testing).

    This just calls send_request_to_all() multiple times.
    In a real implementation, we'd check bloom convergence, but for testing
    we just do multiple rounds.

    Args:
        t_ms: Base timestamp
        db: Database connection
        max_rounds: Maximum rounds before giving up (default 100)

    Returns:
        Number of rounds completed
    """
    log.warning(f"[SYNC_DEBUG] send_request_to_all_debug starting, max_rounds={max_rounds}")

    for round_num in range(max_rounds):
        log.warning(f"[SYNC_DEBUG] Send round {round_num + 1}/{max_rounds}")
        sync.send_request_to_all(t_ms=t_ms + round_num, db=db)

    log.warning(f"[SYNC_DEBUG] Completed {max_rounds} send rounds")
    return max_rounds


def receive_debug(batch_size: int, t_ms: int, db: Any, max_rounds: int = 100) -> int:
    """Keep processing batches and unblocking until stable for multiple rounds.

    Runs until there are no more events to unblock with our existing events.
    There may still be blocked events, but we cannot unblock them.

    Args:
        batch_size: Events to process per receive() call
        t_ms: Base timestamp
        db: Database connection
        max_rounds: Maximum rounds before giving up (default 100)

    Returns:
        Number of rounds completed
    """
    log.warning(f"[SYNC_DEBUG] receive_debug starting, batch_size={batch_size}, max_rounds={max_rounds}")

    stable_rounds = 0
    required_stable_rounds = 3  # Need 3 rounds with no changes to consider stable

    for round_num in range(max_rounds):
        log.warning(f"[SYNC_DEBUG] Round {round_num + 1}/{max_rounds}, stable_rounds={stable_rounds}")

        # Track blocked counts before processing
        before_blocked = {}
        local_peers = db.query("SELECT peer_id FROM local_peers")
        for peer_row in local_peers:
            peer_id = peer_row['peer_id']
            blocked_count = db.query_one(
                "SELECT COUNT(*) as count FROM blocked_events_ephemeral WHERE recorded_by = ?",
                (peer_id,)
            )
            before_blocked[peer_id] = blocked_count['count'] if blocked_count else 0

        # Process incoming events
        sync.receive(batch_size=batch_size, t_ms=t_ms + round_num, db=db)

        # Track blocked counts after processing
        after_blocked = {}
        for peer_row in local_peers:
            peer_id = peer_row['peer_id']
            blocked_count = db.query_one(
                "SELECT COUNT(*) as count FROM blocked_events_ephemeral WHERE recorded_by = ?",
                (peer_id,)
            )
            after_blocked[peer_id] = blocked_count['count'] if blocked_count else 0

        # Check if anything changed
        changed = False
        for peer_id in before_blocked:
            if before_blocked[peer_id] != after_blocked[peer_id]:
                changed = True
                log.warning(f"[SYNC_DEBUG] peer={peer_id[:10]}... blocked: {before_blocked[peer_id]} -> {after_blocked[peer_id]}")

        if not changed:
            stable_rounds += 1
            log.warning(f"[SYNC_DEBUG] No changes in round {round_num + 1}, stable_rounds={stable_rounds}/{required_stable_rounds}")
            if stable_rounds >= required_stable_rounds:
                log.warning(f"[SYNC_DEBUG] Stable for {stable_rounds} rounds, done")
                return round_num + 1
        else:
            stable_rounds = 0

    log.warning(f"[SYNC_DEBUG] Hit max_rounds={max_rounds}, may not have converged")
    return max_rounds
