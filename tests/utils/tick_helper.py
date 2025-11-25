"""Test utility for standardized tick iteration with convergence detection.

Provides consistent defaults for timing across all tests to match production job frequencies,
plus utilities to detect when sync has converged rather than using hard-coded round counts.
"""
from typing import Any
import tick as tick_module


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
    max_rounds: int = 200,
    check_interval: int = 5,
    verbose: bool = False,
    stability_threshold: int = 3
) -> tuple[int, int, bool, dict]:
    """Run ticks until all peer pairs converge, stabilize, or max_rounds reached.

    Checks convergence every check_interval rounds by comparing
    database state (what each peer actually has recorded).

    Exits early when:
    - Full convergence: All peer pairs synced + queue empty
    - Stability: Missing counts unchanged for stability_threshold consecutive checks

    Args:
        db: Database connection
        start_t_ms: Starting timestamp
        max_rounds: Maximum sync rounds (default 200)
        check_interval: Check convergence every N rounds (default 5)
        verbose: Print convergence status (default False)
        stability_threshold: Exit if missing counts stable for N checks (default 3)

    Returns:
        (final_t_ms, rounds_used, converged, status_dict)

    Status dict contains:
        - 'converged': bool (True only if fully converged)
        - 'stable': bool (True if counts stabilized)
        - 'peer_pairs': list of convergence status per pair
        - 'queue_size': incoming_blobs count
        - 'blocked_count': blocked_events count
    """
    from events.network import sync as sync_module

    prev_missing_total = None
    prev_queue_size = None
    stable_count = 0

    for round_num in range(max_rounds):
        t_ms = start_t_ms + (round_num * TICK_INTERVAL_MS)
        tick_module.tick(t_ms=t_ms, db=db)

        # Check convergence every check_interval rounds
        if (round_num + 1) % check_interval == 0:
            status = sync_module.check_all_convergence(db)

            # Calculate total missing across all pairs
            missing_total = sum(p['missing_count'] for p in status['peer_pairs'])

            if verbose:
                print(f"Round {round_num + 1}: "
                      f"converged={status['converged']}, "
                      f"queue={status['queue_size']}, "
                      f"blocked={status['blocked_count']}, "
                      f"missing_total={missing_total}")
                for pair in status['peer_pairs']:
                    if not pair['converged']:
                        print(f"  {pair['from_peer']} → {pair['to_peer']}: "
                              f"missing {pair['missing_count']} events")

            # Check for full convergence
            if status['converged'] and status['queue_size'] == 0:
                final_t_ms = start_t_ms + ((round_num + 1) * TICK_INTERVAL_MS)
                status['stable'] = True
                if verbose:
                    print(f"✓ Fully converged in {round_num + 1} rounds!")
                return (final_t_ms, round_num + 1, True, status)

            # Check for stability (counts not changing)
            # Also track queue stability - some blobs may be stuck (missing keys)
            if prev_missing_total is not None and prev_queue_size is not None:
                queue_stable = (status['queue_size'] == prev_queue_size)
                counts_stable = (missing_total == prev_missing_total)

                if counts_stable and queue_stable:
                    stable_count += 1
                    if stable_count >= stability_threshold:
                        final_t_ms = start_t_ms + ((round_num + 1) * TICK_INTERVAL_MS)
                        status['stable'] = True
                        if verbose:
                            stuck_blobs = f", {status['queue_size']} stuck" if status['queue_size'] > 0 else ""
                            print(f"✓ Stabilized in {round_num + 1} rounds ({missing_total} missing{stuck_blobs})")
                        return (final_t_ms, round_num + 1, False, status)
                else:
                    stable_count = 0

            prev_missing_total = missing_total
            prev_queue_size = status['queue_size']

    # Did not converge or stabilize within max_rounds
    final_t_ms = start_t_ms + (max_rounds * TICK_INTERVAL_MS)
    final_status = sync_module.check_all_convergence(db)
    final_status['stable'] = False
    if verbose:
        print(f"✗ Did not converge/stabilize within {max_rounds} rounds")
        print(f"   Final status: {final_status}")
    return (final_t_ms, max_rounds, False, final_status)
