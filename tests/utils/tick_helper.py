"""Test utility for standardized tick iteration with convergence detection.

Provides consistent defaults for timing across all tests to match production job frequencies,
plus utilities to detect when sync has converged rather than using hard-coded round counts.
"""
from typing import Any, Callable
from core import tick as tick_module


# Global test timeout - increase this if tests are timing out
# 500 rounds = 50 seconds of simulated time at 100ms intervals
DEFAULT_MAX_ROUNDS = 500

# Realistic base timestamp for tests (2025-01-02 00:00:00 UTC)
# This is one day after EPOCH_MS (2025-01-01) to ensure events get proper
# bucket distribution with time-based unified keys in negentropy sync.
TEST_BASE_TIME_MS = 1735776000000

# Monotonic test clock to prevent backwards time across helpers
_TEST_CLOCK_MS = TEST_BASE_TIME_MS


class Clock:
    """A test clock for managing timestamps without magic numbers.

    Usage:
        clock = TestClock()
        alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
        invite = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)
        bob = user.join(..., t_ms=clock.tick(), db=db)

    Each call to tick() returns the current time and advances by default_step_ms.
    Use tick(step=N) to advance by a custom amount.
    """

    def __init__(self, start_ms: int = TEST_BASE_TIME_MS, default_step_ms: int = 1000):
        """Initialize the clock.

        Args:
            start_ms: Starting timestamp (default: TEST_BASE_TIME_MS)
            default_step_ms: Default step size for tick() (default: 1 second)
        """
        self.t_ms = start_ms
        self.default_step_ms = default_step_ms

    def tick(self, step: int = None) -> int:
        """Get current time and advance the clock.

        Args:
            step: How much to advance after returning current time.
                  If None, uses default_step_ms.

        Returns:
            Current timestamp (before advancing)
        """
        current = self.t_ms
        self.t_ms += step if step is not None else self.default_step_ms
        return current

    def now(self) -> int:
        """Get current time without advancing."""
        return self.t_ms

    def advance(self, step_ms: int) -> int:
        """Advance time and return new current time.

        Args:
            step_ms: How much to advance

        Returns:
            New current timestamp (after advancing)
        """
        self.t_ms += step_ms
        return self.t_ms


# Alias for backwards compatibility (TestClock is a clearer name in test code)
TestClock = Clock


def reset_test_clock() -> None:
    """Reset the monotonic test clock (called by test fixtures)."""
    global _TEST_CLOCK_MS
    _TEST_CLOCK_MS = TEST_BASE_TIME_MS


def _coerce_start_t_ms(db: Any, start_t_ms: int | None, interval_ms: int) -> int:
    """Ensure start_t_ms is monotonic and not earlier than job_state."""
    global _TEST_CLOCK_MS

    if start_t_ms is None:
        start_t_ms = _TEST_CLOCK_MS

    latest_created_at = 0
    for table in ("shareable_events", "projected_events"):
        try:
            row = db._conn.execute(f"SELECT MAX(created_at) FROM {table}").fetchone()
        except Exception:
            continue
        if row and row[0] is not None:
            latest_created_at = max(latest_created_at, row[0])

    last_run_row = db._conn.execute("SELECT MAX(last_run_at) FROM job_state").fetchone()
    last_run_at = last_run_row[0] if last_run_row and last_run_row[0] is not None else 0

    start_t_ms = max(start_t_ms or 0, last_run_at, _TEST_CLOCK_MS, latest_created_at)
    return start_t_ms


def _advance_test_clock(t_ms: int, interval_ms: int) -> None:
    """Advance the monotonic test clock to at least t_ms + interval."""
    global _TEST_CLOCK_MS
    _TEST_CLOCK_MS = max(_TEST_CLOCK_MS, t_ms + interval_ms)


def assert_eventually(
    check: Callable[[], Any],
    db: Any,
    start_t_ms: int | None = None,
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
        start_t_ms: Starting timestamp (None = use monotonic test clock)
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
            start_t_ms=None
        )
    """
    if max_rounds is None:
        max_rounds = DEFAULT_MAX_ROUNDS

    last_error = None

    start_t_ms = _coerce_start_t_ms(db, start_t_ms, interval_ms)

    for i in range(max_rounds):
        t_ms = start_t_ms + i * interval_ms
        tick_module.tick(t_ms=t_ms, db=db)
        db.commit()

        try:
            result = check()
            # Support both assertion-style (raises) and return-style (truthy/falsy)
            if result is not None and not result:
                raise AssertionError(f"Check returned falsy: {result}")
            _advance_test_clock(t_ms, interval_ms)
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

    _advance_test_clock(start_t_ms + max_rounds * interval_ms, interval_ms)
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


def run_ticks(db: Any, start_t_ms: int | None, num_rounds: int, interval_ms: int = TICK_INTERVAL_MS) -> int:
    """Run multiple ticks with consistent timing.

    Args:
        db: Database connection
        start_t_ms: Starting timestamp in milliseconds (None = use monotonic test clock)
        num_rounds: Number of tick cycles to run
        interval_ms: Time between ticks (default: 100ms to match sync jobs)

    Returns:
        Final timestamp after all ticks
    """
    start_t_ms = _coerce_start_t_ms(db, start_t_ms, interval_ms)
    for i in range(num_rounds):
        t_ms = start_t_ms + (i * interval_ms)
        tick_module.tick(t_ms=t_ms, db=db)

    _advance_test_clock(start_t_ms + (num_rounds * interval_ms), interval_ms)
    return start_t_ms + (num_rounds * interval_ms)


def initial_sync(db: Any, start_t_ms: int | None = None) -> int:
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


def message_sync(db: Any, start_t_ms: int | None) -> int:
    """Run sync rounds for message propagation.

    Runs enough ticks for messages to propagate through sync protocol.

    Args:
        db: Database connection
        start_t_ms: Starting timestamp

    Returns:
        Final timestamp
    """
    return run_ticks(db, start_t_ms, MESSAGE_SYNC_ROUNDS)


def convergence_sync(db: Any, start_t_ms: int | None, max_rounds: int = CONVERGENCE_ROUNDS) -> int:
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
