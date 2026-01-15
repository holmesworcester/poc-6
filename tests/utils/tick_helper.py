"""Test utility for standardized tick iteration with convergence detection.

Provides consistent defaults for timing across all tests to match production job frequencies,
plus utilities to detect when sync has converged rather than using hard-coded round counts.
"""
from typing import Any, Callable
from core import tick as tick_module


# Global test timeout - increase this if tests are timing out
# 500 rounds = 50 seconds of simulated time at 100ms intervals
DEFAULT_MAX_ROUNDS = 500


def assert_eventually(
    check: Callable[[], Any],
    db: Any,
    start_t_ms: int,
    max_rounds: int = None,
    interval_ms: int = 100,
    msg: str = None
) -> int:
    """Run ticks until check() passes or timeout.

    This is the preferred way to write sync tests. Instead of guessing
    how many rounds sync takes, just assert what you care about and
    let this helper tick until it's true.

    Args:
        check: Callable that raises AssertionError if condition not met.
               Can also return a value - truthy = pass, falsy = fail.
        db: Database connection
        start_t_ms: Starting timestamp
        max_rounds: Maximum ticks before giving up (default: DEFAULT_MAX_ROUNDS)
        interval_ms: Time between ticks (default 100ms)
        msg: Optional message for timeout failure

    Returns:
        Final timestamp after check passed

    Raises:
        AssertionError: If check doesn't pass within max_rounds

    Example:
        # Wait until Bob can see Alice's message
        t_ms = assert_eventually(
            lambda: db.query_one(
                "SELECT * FROM messages WHERE id = ? AND recorded_by = ?",
                (msg_id, bob_peer_id)
            ),
            db=db,
            start_t_ms=5000
        )
    """
    if max_rounds is None:
        max_rounds = DEFAULT_MAX_ROUNDS

    last_error = None

    for i in range(max_rounds):
        t_ms = start_t_ms + i * interval_ms
        tick_module.tick(t_ms=t_ms, db=db)
        db.commit()

        try:
            result = check()
            # Support both assertion-style (raises) and return-style (truthy/falsy)
            if result is not None and not result:
                raise AssertionError(f"Check returned falsy: {result}")
            return t_ms + interval_ms  # Return next available timestamp
        except Exception as e:
            # Catch all exceptions - sync may not have propagated data yet
            # ValueError, KeyError, etc. are all retryable until timeout
            last_error = e
            continue

    # Final attempt with real error
    timeout_msg = msg or f"Condition not met after {max_rounds} rounds ({max_rounds * interval_ms}ms)"
    try:
        result = check()
        if result is not None and not result:
            raise AssertionError(f"{timeout_msg}: check returned {result}")
    except Exception as e:
        raise AssertionError(f"{timeout_msg}: {e}") from None

    return start_t_ms + max_rounds * interval_ms


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
