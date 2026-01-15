"""Test utility for standardized tick iteration with convergence detection.

Provides consistent defaults for timing across all tests to match production job frequencies,
plus utilities to detect when sync has converged rather than using hard-coded round counts.
"""
from typing import Any
from core import tick as tick_module


# Production job frequencies (from jobs.py):
# - SyncReceive/SyncSend: every 100ms
# - SyncConnectSend: every 1000ms (1 second)
# - Other jobs: slower (minutes/hours)

# Test timing constants
TICK_INTERVAL_MS = 100  # Match sync job frequency for realistic timing
INITIAL_SYNC_ROUNDS = 15  # ~1.5 seconds - enough for initial connection + first sync
MESSAGE_SYNC_ROUNDS = 20  # ~2 seconds - enough for message propagation
CONVERGENCE_ROUNDS = 100  # ~10 seconds - for complete event convergence tests


def run_ticks(db: Any, start_t_ms: int, num_rounds: int, interval_ms: int = TICK_INTERVAL_MS) -> int:
    """Run multiple ticks with consistent timing.

    Args:
        db: Database connection
        start_t_ms: Starting timestamp in milliseconds
        num_rounds: Number of tick cycles to run
        interval_ms: Time between ticks (default: 100ms to match sync jobs)

    Returns:
        Final timestamp after all ticks
    """
    for i in range(num_rounds):
        t_ms = start_t_ms + (i * interval_ms)
        tick_module.tick(t_ms=t_ms, db=db)

    return start_t_ms + (num_rounds * interval_ms)


def initial_sync(db: Any, start_t_ms: int = 4000) -> int:
    """Run initial sync rounds for connection establishment.

    Runs enough ticks for:
    - Connection announcement (1 second)
    - Initial sync request/response (multiple 100ms cycles)
    - GKS event propagation

    Args:
        db: Database connection
        start_t_ms: Starting timestamp (default: 4000ms, after network creation at ~2000ms)

    Returns:
        Final timestamp
    """
    return run_ticks(db, start_t_ms, INITIAL_SYNC_ROUNDS)


def message_sync(db: Any, start_t_ms: int) -> int:
    """Run sync rounds for message propagation.

    Runs enough ticks for messages to propagate through sync protocol.

    Args:
        db: Database connection
        start_t_ms: Starting timestamp

    Returns:
        Final timestamp
    """
    return run_ticks(db, start_t_ms, MESSAGE_SYNC_ROUNDS)


def convergence_sync(db: Any, start_t_ms: int, max_rounds: int = CONVERGENCE_ROUNDS) -> int:
    """Run sync rounds for complete event convergence.

    Used in infrastructure tests that verify all events sync.

    Args:
        db: Database connection
        start_t_ms: Starting timestamp
        max_rounds: Maximum rounds to run (default: 100 for ~10 seconds)

    Returns:
        Final timestamp
    """
    return run_ticks(db, start_t_ms, max_rounds)


def sync_until_converged(
    db: Any,
    start_t_ms: int,
    max_rounds: int = 500,
    check_interval: int = 5,
    verbose: bool = False,
    stability_threshold: int = 200
) -> tuple[int, int, bool, dict]:
    """Run ticks until sync stabilizes (no more progress) or max_rounds reached.

    Uses snapshot-based detection: takes a snapshot of recorded event counts
    at the start, then checks if sync is still making progress. Exits when
    no new events have been recorded for stability_threshold consecutive checks.

    This approach avoids complex peer-pair detection and naturally handles
    all sync scenarios including multi-device and cross-network cases.

    Args:
        db: Database connection
        start_t_ms: Starting timestamp
        max_rounds: Maximum sync rounds (default 500)
        check_interval: Check progress every N rounds (default 5)
        verbose: Print progress status (default False)
        stability_threshold: Exit if no progress for N consecutive checks (default 200)

    Returns:
        (final_t_ms, rounds_used, converged, status_dict)

        rounds_used is the round when stability STARTED (not when we confirmed it),
        so it reflects when sync actually completed, not the verification period.

    Status dict contains:
        - 'converged': bool (True if queue empty when stabilized)
        - 'stable': bool (True if counts stabilized)
        - 'queue_size': incoming_blobs count
        - 'blocked_count': blocked_events count
        - 'total_valid': total valid events across all peers
    """
    from events.network import sync as sync_module

    # Take initial snapshot
    snapshot = sync_module.take_sync_snapshot(db)
    stable_count = 0
    stability_started_round = None  # Track when we first became stable
    prev_total_valid = sum(snapshot['valid_counts'].values())

    for round_num in range(max_rounds):
        t_ms = start_t_ms + (round_num * TICK_INTERVAL_MS)
        tick_module.tick(t_ms=t_ms, db=db)

        # Check progress every check_interval rounds
        if (round_num + 1) % check_interval == 0:
            status = sync_module.check_sync_progress(db, snapshot)
            snapshot = status['snapshot']  # Update snapshot for next check

            if verbose:
                new_events = status['total_valid'] - prev_total_valid
                print(f"Round {round_num + 1}: "
                      f"valid={status['total_valid']} (+{new_events}), "
                      f"queue={status['queue_size']}, "
                      f"blocked={status['blocked_count']}")

            # Check for stability (no progress for N consecutive checks)
            if not status['progressed']:
                if stable_count == 0:
                    # First stable check - record when stability started
                    stability_started_round = round_num + 1
                stable_count += 1
                if stable_count >= stability_threshold:
                    # Report the round when stability STARTED, not when confirmed
                    final_t_ms = start_t_ms + (stability_started_round * TICK_INTERVAL_MS)
                    converged = (status['queue_size'] == 0)
                    if verbose:
                        stuck = f", {status['queue_size']} stuck in queue" if status['queue_size'] > 0 else ""
                        print(f"✓ Stabilized at round {stability_started_round} "
                              f"(confirmed after {stable_count} checks, "
                              f"{status['total_valid']} total valid{stuck})")
                    return (final_t_ms, stability_started_round, converged, {
                        'converged': converged,
                        'stable': True,
                        'queue_size': status['queue_size'],
                        'blocked_count': status['blocked_count'],
                        'total_valid': status['total_valid']
                    })
            else:
                stable_count = 0
                stability_started_round = None

            prev_total_valid = status['total_valid']

    # Did not stabilize within max_rounds
    final_t_ms = start_t_ms + (max_rounds * TICK_INTERVAL_MS)
    final_status = sync_module.check_sync_progress(db, snapshot)
    if verbose:
        print(f"✗ Did not stabilize within {max_rounds} rounds")
    return (final_t_ms, max_rounds, False, {
        'converged': False,
        'stable': False,
        'queue_size': final_status['queue_size'],
        'blocked_count': final_status['blocked_count'],
        'total_valid': final_status['total_valid']
    })
