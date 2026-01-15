"""Wall-clock tick runner for performance testing.

This module provides functions to run tick() at actual wall-clock intervals,
measuring execution time and detecting backpressure (when ticks take longer
than their interval).

Usage:
    from tests import perf_tick

    # Run until condition met
    final_t_ms, report = perf_tick.run_realtime(
        db=db,
        start_t_ms=1000,
        until_condition=lambda t, db: some_check(t, db)
    )

    # Run N ticks at wall-clock time
    final_t_ms, report = perf_tick.run_ticks_timed(
        db=db,
        start_t_ms=1000,
        num_ticks=100,
        realtime=True
    )

    print(report.summary())
"""
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from core import tick as tick_module

TICK_INTERVAL_MS = 100  # Production interval


@dataclass
class PerfReport:
    """After-action report from a perf test run."""
    total_ticks: int = 0
    ticks_exceeded: int = 0
    max_tick_time_ms: float = 0
    total_wall_time_sec: float = 0
    total_sim_time_ms: int = 0
    tick_times: list[float] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            "Performance Report",
            "==================",
            f"Wall time:     {self.total_wall_time_sec:.2f}s",
            f"Sim time:      {self.total_sim_time_ms}ms ({self.total_sim_time_ms/1000:.1f}s)",
            f"Total ticks:   {self.total_ticks}",
            f"Ticks > 100ms: {self.ticks_exceeded} ({100*self.ticks_exceeded/max(1,self.total_ticks):.1f}%)",
            f"Max tick time: {self.max_tick_time_ms:.1f}ms",
        ]
        if self.tick_times:
            avg = sum(self.tick_times) / len(self.tick_times)
            lines.append(f"Avg tick time: {avg:.1f}ms")
        return "\n".join(lines)


def run_realtime(
    db: Any,
    start_t_ms: int,
    until_condition: Callable[[int, Any], bool],
    max_ticks: int = 10000,
    record_all_ticks: bool = False
) -> tuple[int, PerfReport]:
    """Run ticks at wall-clock time until condition is met.

    Each tick is followed by a sleep to maintain 100ms intervals. If a tick
    takes longer than 100ms, no sleep occurs and the next tick starts
    immediately (backpressure).

    Args:
        db: Database connection
        start_t_ms: Starting simulation time
        until_condition: Callable(t_ms, db) -> bool, returns True when done
        max_ticks: Safety limit to prevent infinite loops
        record_all_ticks: If True, record every tick time (uses more memory)

    Returns:
        (final_t_ms, PerfReport)
    """
    report = PerfReport()
    t_ms = start_t_ms
    wall_start = time.perf_counter()

    for tick_num in range(max_ticks):
        tick_start = time.perf_counter()

        # Execute tick
        tick_module.tick(t_ms=t_ms, db=db)
        db.commit()

        tick_end = time.perf_counter()
        tick_time_ms = (tick_end - tick_start) * 1000

        # Record metrics
        report.total_ticks += 1
        if tick_time_ms > TICK_INTERVAL_MS:
            report.ticks_exceeded += 1
        report.max_tick_time_ms = max(report.max_tick_time_ms, tick_time_ms)
        if record_all_ticks:
            report.tick_times.append(tick_time_ms)

        # Check completion condition
        if until_condition(t_ms, db):
            break

        # Sleep to maintain wall-clock timing
        elapsed_ms = tick_time_ms
        sleep_ms = max(0, TICK_INTERVAL_MS - elapsed_ms)
        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000)

        t_ms += TICK_INTERVAL_MS

    wall_end = time.perf_counter()
    report.total_wall_time_sec = wall_end - wall_start
    report.total_sim_time_ms = t_ms - start_t_ms

    return t_ms, report


def run_ticks_timed(
    db: Any,
    start_t_ms: int,
    num_ticks: int,
    realtime: bool = True
) -> tuple[int, PerfReport]:
    """Run N ticks, optionally at wall-clock time.

    Args:
        db: Database connection
        start_t_ms: Starting simulation time
        num_ticks: Number of ticks to run
        realtime: If True, sleep between ticks to maintain 100ms intervals.
                  If False, run as fast as possible (still measures time).

    Returns:
        (final_t_ms, PerfReport)
    """
    ticks_done = [0]

    def until_done(t_ms: int, db: Any) -> bool:
        ticks_done[0] += 1
        return ticks_done[0] >= num_ticks

    if realtime:
        return run_realtime(db, start_t_ms, until_done, max_ticks=num_ticks)
    else:
        # Fast mode - no sleeping, just measure execution time
        report = PerfReport()
        t_ms = start_t_ms
        wall_start = time.perf_counter()

        for _ in range(num_ticks):
            tick_start = time.perf_counter()
            tick_module.tick(t_ms=t_ms, db=db)
            db.commit()
            tick_end = time.perf_counter()

            tick_time_ms = (tick_end - tick_start) * 1000
            report.total_ticks += 1
            if tick_time_ms > TICK_INTERVAL_MS:
                report.ticks_exceeded += 1
            report.max_tick_time_ms = max(report.max_tick_time_ms, tick_time_ms)

            t_ms += TICK_INTERVAL_MS

        report.total_wall_time_sec = time.perf_counter() - wall_start
        report.total_sim_time_ms = t_ms - start_t_ms
        return t_ms, report
