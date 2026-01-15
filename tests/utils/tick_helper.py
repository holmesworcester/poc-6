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
    check_interval: int = 1,
    verbose: bool = False,
    stability_threshold: int = 3,  # Reduced: negentropy detection is deterministic
    sync_only: bool = True  # Use sync-only ticks (no event creation)
) -> tuple[int, int, bool, dict]:
    """Run ticks until all connections are synced or max_rounds reached.

    Uses negentropy-based detection: checks if all active connections have
    matching root hashes. This is deterministic - no need for long stability
    thresholds.

    By default uses sync-only ticks to prevent event-creating jobs from
    causing infinite sync loops.

    Args:
        db: Database connection
        start_t_ms: Starting timestamp
        max_rounds: Maximum sync rounds (default 500)
        check_interval: Check progress every N rounds (default 1)
        verbose: Print progress status (default False)
        stability_threshold: Confirm sync for N consecutive checks (default 3)
        sync_only: Use sync-only ticks, no event creation (default True)

    Returns:
        (final_t_ms, rounds_used, converged, status_dict)

    Status dict contains:
        - 'converged': bool (True if all synced and queue empty)
        - 'all_synced': bool (True if all connections synced)
        - 'queue_empty': bool (True if no pending blobs)
        - 'total_connections': count of active connections
        - 'synced_connections': count of synced connections
    """
    from events.network import sync as sync_module

    tick_fn = tick_module.tick_sync_only if sync_only else tick_module.tick
    synced_count = 0

    for round_num in range(max_rounds):
        t_ms = start_t_ms + (round_num * TICK_INTERVAL_MS)
        tick_fn(t_ms=t_ms, db=db)

        # Check progress every check_interval rounds
        if (round_num + 1) % check_interval == 0:
            status = sync_module.get_global_sync_status(db, t_ms)

            if verbose:
                synced = status['synced_connections']
                total = status['total_connections']
                queue = 'empty' if status['queue_empty'] else 'pending'
                print(f"Round {round_num + 1}: {synced}/{total} connections synced, queue={queue}")

            # Check if fully synced
            if status['all_synced'] and status['queue_empty']:
                synced_count += 1
                if synced_count >= stability_threshold:
                    final_t_ms = start_t_ms + ((round_num + 1) * TICK_INTERVAL_MS)
                    if verbose:
                        print(f"✓ Converged at round {round_num + 1}")
                    return (final_t_ms, round_num + 1, True, {
                        'converged': True,
                        'all_synced': True,
                        'queue_empty': True,
                        'total_connections': status['total_connections'],
                        'synced_connections': status['synced_connections'],
                    })
            else:
                synced_count = 0

    # Did not converge within max_rounds
    final_t_ms = start_t_ms + (max_rounds * TICK_INTERVAL_MS)
    final_status = sync_module.get_global_sync_status(db, final_t_ms)
    if verbose:
        print(f"✗ Did not converge within {max_rounds} rounds")
    return (final_t_ms, max_rounds, False, {
        'converged': False,
        'all_synced': final_status['all_synced'],
        'queue_empty': final_status['queue_empty'],
        'total_connections': final_status['total_connections'],
        'synced_connections': final_status['synced_connections'],
    })
