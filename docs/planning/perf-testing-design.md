# True Performance Testing Framework Design

## Problem Statement

Current scenario tests use **simulated time**: `tick()` is called with explicit `t_ms` parameters, allowing thousands of "milliseconds" to pass instantly. This is excellent for functional testing but doesn't answer:

1. **Can the system keep up with real-time?** - Do operations complete within their tick interval?
2. **What happens under load?** - Do large unblock cascades or file transfers cause backpressure?
3. **What's the realistic user experience?** - How long does a 1GB file actually take to sync?

## Goals

1. **Wall-clock tick mode** - `tick()` runs at actual 100ms intervals
2. **Backpressure detection** - Flag when tick execution exceeds interval
3. **Realistic network simulation** - Add bandwidth limiting (configured separately)
4. **Real disk I/O** - File-based SQLite with WAL mode
5. **After-action reports** - Show timing results after completion

---

## Design Principles

1. **No threading** - Progress recording is fast, run synchronously
2. **Global bandwidth** - Single token bucket, upgrade to per-peer later if needed
3. **After-action reports only** - No live progress callbacks, just final report
4. **Time tolerances** - Tests pass/fail based on completion time ranges
5. **Separation of concerns** - Network config is separate from sync-realtime command

---

## Implementation

### 1. Wall-Clock Tick Runner

New module: `perf_tick.py`

```python
"""Wall-clock tick runner for performance testing."""
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import tick as tick_module

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
            f"Performance Report",
            f"==================",
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

    Args:
        db: Database connection
        start_t_ms: Starting simulation time
        until_condition: Callable(t_ms, db) -> bool, returns True when done
        max_ticks: Safety limit
        record_all_ticks: If True, record every tick time (memory intensive)

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

        # Check completion
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
        realtime: If True, sleep between ticks to maintain 100ms intervals

    Returns:
        (final_t_ms, PerfReport)
    """
    ticks_done = [0]

    def until_done(t_ms, db):
        ticks_done[0] += 1
        return ticks_done[0] >= num_ticks

    if realtime:
        return run_realtime(db, start_t_ms, until_done, max_ticks=num_ticks)
    else:
        # Fast mode - no sleeping, just measure
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
```

### 2. Bandwidth Simulation

Add to `network_config.py`:

```python
# Add to existing file - bandwidth state and calculation

# Global bandwidth state (token bucket)
_bandwidth_tokens = 0
_bandwidth_last_refill_ms = 0


def reset_bandwidth_state():
    """Reset bandwidth limiter state (called by reset_network_config)."""
    global _bandwidth_tokens, _bandwidth_last_refill_ms
    _bandwidth_tokens = 0
    _bandwidth_last_refill_ms = 0


def calculate_delivery_time(size_bytes: int, t_ms: int) -> int:
    """Calculate when a packet should be delivered, accounting for bandwidth.

    Uses token bucket: tokens refill at bandwidth rate, packet waits until
    enough tokens accumulate.

    Args:
        size_bytes: Packet size
        t_ms: Current simulation time

    Returns:
        Delivery time (t_ms + latency + bandwidth_delay)
    """
    global _bandwidth_tokens, _bandwidth_last_refill_ms
    cfg = get_network_config()

    # Base latency with jitter
    latency = calculate_latency()

    # If no bandwidth limit, just use latency
    if cfg.bandwidth_bytes_per_sec is None:
        return t_ms + latency

    # Refill tokens since last packet
    if _bandwidth_last_refill_ms > 0:
        elapsed_ms = t_ms - _bandwidth_last_refill_ms
        refill = int(cfg.bandwidth_bytes_per_sec * elapsed_ms / 1000)
        # Cap at 1 second worth of tokens (burst allowance)
        _bandwidth_tokens = min(
            _bandwidth_tokens + refill,
            cfg.bandwidth_bytes_per_sec
        )
    _bandwidth_last_refill_ms = t_ms

    # Calculate wait time if not enough tokens
    if size_bytes <= _bandwidth_tokens:
        _bandwidth_tokens -= size_bytes
        bandwidth_delay = 0
    else:
        bytes_needed = size_bytes - _bandwidth_tokens
        bandwidth_delay = int(bytes_needed * 1000 / cfg.bandwidth_bytes_per_sec)
        _bandwidth_tokens = 0

    return t_ms + latency + bandwidth_delay


# Update reset_network_config to also reset bandwidth:
def reset_network_config() -> None:
    """Reset to default configuration (for testing)."""
    global _config, _burst_loss_remaining
    _config = NetworkConfig()
    _burst_loss_remaining = 0
    reset_bandwidth_state()  # ADD THIS LINE
```

Modify `queues.py` incoming.add():

```python
# Change this line:
deliver_at = t_ms + latency

# To this:
deliver_at = network_config.calculate_delivery_time(len(blob), t_ms)
```

### 3. Network Profiles (Separate Config)

New file: `network_profiles.py`

```python
"""Realistic network condition profiles.

Usage:
    import network_profiles
    network_profiles.apply('wifi')  # Apply WiFi conditions
    network_profiles.apply('cable')  # Switch to cable
"""
from network_config import NetworkConfig, set_network_config

PROFILES = {
    'instant': NetworkConfig(
        latency_ms=0,
        bandwidth_bytes_per_sec=None,  # Unlimited
    ),
    'lan': NetworkConfig(
        latency_ms=1,
        bandwidth_bytes_per_sec=125_000_000,  # 1 Gbps
    ),
    'wifi': NetworkConfig(
        latency_ms=5,
        jitter_ms=2,
        bandwidth_bytes_per_sec=6_250_000,  # 50 Mbps
        packet_loss_rate=0.001,
    ),
    'cable': NetworkConfig(
        latency_ms=20,
        jitter_ms=5,
        bandwidth_bytes_per_sec=12_500_000,  # 100 Mbps
    ),
    '4g': NetworkConfig(
        latency_ms=50,
        jitter_ms=20,
        bandwidth_bytes_per_sec=2_500_000,  # 20 Mbps
        packet_loss_rate=0.005,
    ),
    '3g': NetworkConfig(
        latency_ms=100,
        jitter_ms=30,
        bandwidth_bytes_per_sec=500_000,  # 4 Mbps
        packet_loss_rate=0.01,
    ),
    'satellite': NetworkConfig(
        latency_ms=600,
        jitter_ms=50,
        bandwidth_bytes_per_sec=1_250_000,  # 10 Mbps
        packet_loss_rate=0.005,
    ),
}


def apply(name: str) -> NetworkConfig:
    """Apply a network profile by name."""
    if name not in PROFILES:
        raise ValueError(f"Unknown profile: {name}. Available: {list(PROFILES.keys())}")
    config = PROFILES[name]
    set_network_config(config)
    return config


def theoretical_transfer_time(size_bytes: int, profile_name: str) -> float:
    """Calculate theoretical minimum transfer time for a profile."""
    config = PROFILES[profile_name]
    if config.bandwidth_bytes_per_sec is None:
        return 0.0
    return size_bytes / config.bandwidth_bytes_per_sec
```

### 4. File-Based SQLite Fixture

Add to `conftest.py`:

```python
@pytest.fixture
def perf_db(tmp_path):
    """File-based database for performance testing."""
    db_path = tmp_path / "perf_test.db"
    conn = sqlite3.connect(str(db_path))
    db = Database(conn)

    # Performance optimizations (WAL already in Database.__init__)
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -64000")  # 64MB
    conn.execute("PRAGMA temp_store = MEMORY")

    schema.create_all(db)
    yield db
    conn.close()
```

### 5. CLI: sync-realtime Command

Add to `cli.py`:

```python
def cmd_sync_realtime(session: CLISession, args: list[str]):
    """Run sync at wall-clock time with after-action report.

    Usage:
        sync-realtime                   # Sync until converged
        sync-realtime --ticks <n>       # Run N ticks
        sync-realtime --until-file <n>  # Sync until file N complete
    """
    import perf_tick
    from events.network import sync_file

    # Parse args
    num_ticks = None
    file_num = None

    i = 0
    while i < len(args):
        if args[i] == '--ticks' and i + 1 < len(args):
            num_ticks = int(args[i + 1])
            i += 2
        elif args[i] == '--until-file' and i + 1 < len(args):
            file_num = int(args[i + 1])
            i += 2
        else:
            i += 1

    # Run based on mode
    if file_num is not None:
        if not hasattr(session, 'file_list') or not session.file_list:
            print("Error: run 'files' first to see file list")
            return
        if not (1 <= file_num <= len(session.file_list)):
            print(f"Error: file #{file_num} not found")
            return

        file_info = session.file_list[file_num - 1]
        file_id = file_info['file_id']
        account = session.get_selected_account()

        print(f"Syncing until file complete: {file_info['filename']}")
        print("Running at wall-clock time...\n")

        def until_done(t_ms, db):
            return sync_file.is_file_complete(file_id, account.peer_id, db)

        final_t_ms, report = perf_tick.run_realtime(
            db=session.db,
            start_t_ms=session.current_time_ms,
            until_condition=until_done,
            max_ticks=100000
        )

    elif num_ticks is not None:
        print(f"Running {num_ticks} ticks at wall-clock time...")
        print(f"(Expected: ~{num_ticks * 0.1:.1f} seconds)\n")

        final_t_ms, report = perf_tick.run_ticks_timed(
            db=session.db,
            start_t_ms=session.current_time_ms,
            num_ticks=num_ticks,
            realtime=True
        )

    else:
        # Default: sync until converged
        print("Syncing until converged (wall-clock time)...\n")

        from tests.utils.tick_helper import sync_until_converged
        final_t_ms, rounds, converged, _ = sync_until_converged(
            db=session.db,
            start_t_ms=session.current_time_ms,
            max_rounds=1000
        )

        # Simple report for convergence mode
        report = perf_tick.PerfReport()
        report.total_ticks = rounds
        report.total_sim_time_ms = final_t_ms - session.current_time_ms

        if not converged:
            print("Warning: did not fully converge")

    session.current_time_ms = final_t_ms

    # Print report
    print("\n" + "=" * 40)
    print(report.summary())
    print("=" * 40)


# Add to command dispatch:
elif cmd == "sync-realtime":
    cmd_sync_realtime(session, parts[1:])

# Add to help:
print("    sync-realtime [--ticks N] [--until-file N]  Sync at wall-clock time")
```

### 6. CLI: network Command (Separate)

```python
def cmd_network(session: CLISession, args: list[str]):
    """Configure network simulation.

    Usage:
        network                     # Show current config
        network <profile>           # Apply profile (instant, lan, wifi, cable, 4g, 3g, satellite)
        network --latency <ms>      # Set custom latency
        network --bandwidth <Mbps>  # Set custom bandwidth
        network --loss <rate>       # Set packet loss (0.0-1.0)
    """
    import network_config
    import network_profiles

    if not args:
        # Show current config
        cfg = network_config.get_network_config()
        print("Current network config:")
        print(f"  Latency:    {cfg.latency_ms}ms (jitter: {cfg.jitter_ms}ms)")
        if cfg.bandwidth_bytes_per_sec:
            mbps = cfg.bandwidth_bytes_per_sec * 8 / 1_000_000
            print(f"  Bandwidth:  {mbps:.0f} Mbps")
        else:
            print(f"  Bandwidth:  unlimited")
        print(f"  Loss rate:  {cfg.packet_loss_rate*100:.1f}%")
        print(f"\nProfiles: {', '.join(network_profiles.PROFILES.keys())}")
        return

    # Check for profile name
    if args[0] in network_profiles.PROFILES:
        config = network_profiles.apply(args[0])
        mbps = config.bandwidth_bytes_per_sec * 8 / 1_000_000 if config.bandwidth_bytes_per_sec else 'unlimited'
        print(f"Applied '{args[0]}': {config.latency_ms}ms latency, {mbps} Mbps")
        return

    # Custom settings
    cfg = network_config.get_network_config()
    i = 0
    while i < len(args):
        if args[i] == '--latency' and i + 1 < len(args):
            cfg.latency_ms = int(args[i + 1])
            i += 2
        elif args[i] == '--bandwidth' and i + 1 < len(args):
            mbps = float(args[i + 1])
            cfg.bandwidth_bytes_per_sec = int(mbps * 1_000_000 / 8)
            i += 2
        elif args[i] == '--loss' and i + 1 < len(args):
            cfg.packet_loss_rate = float(args[i + 1])
            i += 2
        else:
            print(f"Unknown option: {args[i]}")
            return

    network_config.set_network_config(cfg)
    print("Network config updated")


# Add to command dispatch:
elif cmd == "network":
    cmd_network(session, parts[1:])

# Add to help:
print("    network [profile|--latency|--bandwidth]    Configure network simulation")
```

---

## Example CLI Session

```
> network
Current network config:
  Latency:    0ms (jitter: 0ms)
  Bandwidth:  unlimited
  Loss rate:  0.0%

Profiles: instant, lan, wifi, cable, 4g, 3g, satellite

> network cable
Applied 'cable': 20ms latency, 100 Mbps

> send-with-gb
✓ sent message with 1.00 GB file (2,330,169 slices)

> files
IN PROGRESS:
    1. ↓ test_1gb.bin          [░░░░░░░░░░]   0%

> sync-realtime --until-file 1
Syncing until file complete: test_1gb.bin
Running at wall-clock time...

========================================
Performance Report
==================
Wall time:     142.38s
Sim time:      142400ms (142.4s)
Total ticks:   1424
Ticks > 100ms: 12 (0.8%)
Max tick time: 187.3ms
========================================

> files
COMPLETE:
    1. ✓ test_1gb.bin          1.00 GB
```

---

### 7. CLI: File-Based Database for Performance Testing

The CLI currently uses `:memory:` which bypasses real disk I/O. For true perf testing, we need file-based SQLite.

Modify `CLISession.initialize_database()` in `cli.py`:

```python
def initialize_database(self, use_file: bool = False, db_path: str = None):
    """Initialize database with schema.

    Args:
        use_file: If True, use file-based SQLite for realistic I/O
        db_path: Custom path, or None for temp file (deleted on exit)
    """
    if use_file:
        if db_path is None:
            # Create temp file that persists for session
            import tempfile
            self._db_file = tempfile.NamedTemporaryFile(
                prefix='poc6_cli_',
                suffix='.db',
                delete=False
            )
            db_path = self._db_file.name
            print(f"Using database: {db_path}")

        conn = sqlite3.connect(db_path)
        # Performance optimizations
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
        conn.execute("PRAGMA temp_store = MEMORY")
    else:
        conn = sqlite3.Connection(":memory:")

    self.db = Database(conn)
    schema.create_all(self.db)
```

Add CLI flag in `main()`:

```python
parser.add_argument('--disk', action='store_true',
                    help='Use file-based SQLite for realistic I/O perf')
parser.add_argument('--db-path', type=str, default=None,
                    help='Path to database file (implies --disk)')

# In main():
use_disk = args.disk or args.db_path is not None
session.initialize_database(use_file=use_disk, db_path=args.db_path)
```

Usage:
```bash
# In-memory (fast, default)
python cli.py

# File-based (realistic I/O, temp file)
python cli.py --disk

# File-based with specific path (for inspection)
python cli.py --db-path /tmp/my_test.db
```

### 8. CLI: send --repeat for Bulk Message Generation

Add `--repeat` flag to send command for stress testing:

```python
def cmd_send(session: CLISession, args: list[str]):
    """Send a message to the current channel.

    Usage:
        send <message>              # Send single message
        send --repeat <n> <message> # Send n messages
    """
    if not args:
        print("usage: send <message> [--repeat n]")
        return

    # Parse --repeat flag
    repeat = 1
    if '--repeat' in args:
        idx = args.index('--repeat')
        if idx + 1 < len(args):
            repeat = int(args[idx + 1])
            args = args[:idx] + args[idx+2:]  # Remove flag and value

    content = ' '.join(args)

    account = session.get_selected_account()
    if not session.selected_channel_id:
        print("error: no channel selected")
        return

    print(f"Sending {repeat} message(s)...")

    for i in range(repeat):
        msg_content = content if repeat == 1 else f"{content} [{i+1}/{repeat}]"

        message.create(
            peer_id=account.peer_id,
            channel_id=session.selected_channel_id,
            content=msg_content,
            t_ms=session.current_time_ms,
            db=session.db
        )
        session.current_time_ms += 10  # Small time increment per message

    session.db.commit()
    print(f"✓ sent {repeat} message(s)")

    session.run_auto_tick()
```

Usage:
```
> send hello world
✓ sent 1 message(s)

> send --repeat 100 stress test message
Sending 100 message(s)...
✓ sent 100 message(s)

> send --repeat 1000 bulk test
Sending 1000 message(s)...
✓ sent 1000 message(s)
```

This is useful for:
- Testing sync performance with many events
- Stress testing unblock cascades
- Benchmarking database write performance

---

## Implementation Order

1. **`perf_tick.py`** - Wall-clock runner with PerfReport
2. **`network_config.py`** - Add bandwidth token bucket + `calculate_delivery_time()`
3. **`queues.py`** - Use `calculate_delivery_time()`
4. **`network_profiles.py`** - Profile definitions
5. **`conftest.py`** - Add `perf_db` fixture
6. **`cli.py`** - Changes:
   - `--disk` / `--db-path` flags for file-based DB
   - `network` command for network config
   - `sync-realtime` command
   - `send --repeat` for bulk messages

---

## Example Performance Testing Session

```bash
# Start CLI with file-based database
$ python cli.py --disk
Using database: /tmp/poc6_cli_abc123.db

> new-network --name Alice --username alice
✓ created network 'TestNet' as alice (desktop)

> invite
✓ created invite #1

> accept-invite --invite 1 --username bob
✓ bob (desktop) joined TestNet

> network cable
Applied 'cable': 20ms latency, 100 Mbps

> send --repeat 1000 stress test
Sending 1000 message(s)...
✓ sent 1000 message(s)

> sync-realtime
Syncing until converged (wall-clock time)...

========================================
Performance Report
==================
Wall time:     45.23s
Sim time:      45200ms (45.2s)
Total ticks:   452
Ticks > 100ms: 23 (5.1%)
Max tick time: 234.5ms
========================================
```

---

## Test Assertions

Performance tests assert on time ranges:

```python
@pytest.mark.perf
def test_1mb_wifi(perf_db):
    # ... setup ...
    network_profiles.apply('wifi')

    final_t_ms, report = perf_tick.run_realtime(...)

    # 1MB at 50 Mbps = 0.16s theoretical, allow 30s max
    assert report.total_wall_time_sec < 30
    assert report.ticks_exceeded < report.total_ticks * 0.05  # <5% backpressure
```
