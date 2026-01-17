# Plan: Unified Packet Queue + Server Mode

## Overview

This document describes two enhancements:

1. **Unified Packet Queue** - Single SQLite-based queue for all packet sources
2. **Server Mode** - Always-on servers that accept QUIC/WebSocket connections

---

## Part 1: Unified Packet Queue

### Problem Statement

The current architecture has the simulator maintaining its own in-memory queue, separate from SQLite:

```
Current (broken for real networking):

  Simulator.send() ──► in-memory _delivered list ──► drain()
                              ▲
  UDP recv ──► sim.inject() ──┘   (global list = cross-client interference)
```

### Key Insight

The schema already has `incoming_blobs` table:

```sql
CREATE TABLE IF NOT EXISTS incoming_blobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blob BLOB NOT NULL,
    sent_at INTEGER NOT NULL,
    deliver_at INTEGER NOT NULL DEFAULT 0,
    dropped BOOLEAN DEFAULT FALSE
);
```

### Database Model

There are two modes of operation with different database models:

**Real Networking (separate databases per instance)**:
- Each running CLI instance or test client has its own database file
- Real networking tests: Alice's `Client` has `alice.db`, Bob's `Client` has `bob.db`
- CLI usage: `python cli.py --db alice.db` on one machine, `python cli.py --db bob.db` on another
- Packets flow over real UDP/QUIC/WebSocket between instances
- Isolation is automatic: each database has its own `incoming_blobs` table

**Simulator / Scenario Tests (single database, multiple peers)**:
- One process, one database, many peers simulated within it
- Peers are differentiated by `peer_id` with peer-scoped views
- The simulator handles "delivery" between peers (same db, no real network)
- All peers share one `incoming_blobs` queue

**In both modes**: A database can contain multiple peers/identities (e.g., personal + work accounts). When packets are drained, `unwrap_and_store()` routes each packet to whichever peer(s) can decrypt it based on transit wrap encryption. The queue doesn't need to know about peers - the crypto layer handles routing.

### Unified Design

```
                    ┌─────────────────────────────────┐
                    │     incoming_blobs table        │
                    │  (one per db = one per client)  │
                    └─────────────────────────────────┘
                              ▲           │
                              │           │ drain()
            ┌─────────────────┴───┐       │ SELECT WHERE deliver_at <= t_ms
            │                     │       ▼
     Simulator.send()        UDP recv    sync.receive()
     (models latency,        (immediate)
      loss, NAT, etc)
     INSERT with             INSERT with
     deliver_at=t+latency    deliver_at=t
```

**No fallback logic. No special paths. One queue, multiple sources.**

- **Simulator**: Models network physics (latency, loss, partitions, NAT), then INSERTs surviving packets into `incoming_blobs` with `deliver_at = t_ms + latency`
- **Real networking**: UDP/QUIC/WebSocket recv → INSERT into `incoming_blobs` with `deliver_at = t_ms` (immediate)
- **drain()**: Always reads from `incoming_blobs` table: `SELECT ... WHERE deliver_at <= ? AND NOT dropped`

### Why This Works

1. **Per-client isolation**: Each client has own db → own queue table → no interference
2. **Batching preserved**: drain() returns batch, sync.receive() processes batch
3. **One code path**: drain() always reads from SQLite, doesn't care where packets came from
4. **Simulator becomes stateless**: Just calculates physics (latency, loss), doesn't store packets

### How Routing Works (Transit Wrap)

A potential concern: "In scenario tests with shared db, what if Alice's drain() runs before Bob's and takes Bob's packet?"

**This is not a problem.** Here's why:

1. **One drain() per tick**: In scenario tests, there's ONE `tick()` call that processes ALL peers. The `sync_receive` job calls `drain()` once, not per-peer.

2. **Routing via decryption**: When Alice sends to Bob, the packet is transit-wrapped (encrypted) for Bob's keys. When `drain()` returns packets, `unwrap_and_store()` tries to decrypt each one. Alice can't decrypt Bob's packet - she doesn't have his keys - so she ignores it.

3. **Same as current behavior**: The in-memory simulator also doesn't filter by recipient. Routing has always been handled by the transit wrap encryption layer, not the queue.

### Implementation

#### Phase 1: Simulator becomes stateless physics calculator

The simulator doesn't need database access. It just calculates whether a packet should be dropped and when it should arrive. The calling code (`queues.incoming.add()`) already has `unsafedb` and does the INSERT.

**File: `simulator/nspy_network.py`**

```python
def calculate_delivery(self, from_peer: str, to_peer: str, blob: bytes, t_ms: int) -> tuple[bool, int]:
    """Calculate packet fate without storing.

    Returns:
        (should_drop, deliver_at) - if should_drop is True, deliver_at is ignored
    """
    # Check partitions
    if self.is_partitioned(from_peer) or self.is_partitioned(to_peer):
        return (True, 0)

    # Check NAT
    if not self._check_nat_mapping(from_peer, to_peer, t_ms):
        return (True, 0)

    # Apply loss model
    if self._should_drop(from_peer, to_peer, blob):
        return (True, 0)

    # Calculate delivery time
    deliver_at = t_ms + self._calculate_latency()
    return (False, deliver_at)
```

**File: `core/queues.py`** - add() uses simulator for physics, then INSERTs

```python
@staticmethod
def add(blob: bytes, t_ms: int, unsafedb: UnsafeDB, from_peer: str = None, to_peer: str = None) -> bool:
    """Add packet to queue, applying simulated network physics."""

    # Check if transport callback wants to handle this (real networking bypass)
    callback = _transport_callback
    if callback is not None:
        if callback(blob, from_peer or "unknown", to_peer or "unknown", t_ms):
            return True  # Handled by real transport, don't queue

    # Use simulator for physics (latency, loss, NAT, partitions)
    sim = network_config.get_simulator()
    should_drop, deliver_at = sim.calculate_delivery(from_peer, to_peer, blob, t_ms)

    if should_drop:
        return False

    # Write to SQLite queue
    unsafedb.execute(
        "INSERT INTO incoming_blobs (blob, sent_at, deliver_at) VALUES (?, ?, ?)",
        (blob, t_ms, deliver_at)
    )
    return True
```

#### Phase 2: drain() reads from SQLite

**File: `core/queues.py`**

```python
@staticmethod
def drain(batch_size: int, current_time_ms: int, unsafedb: UnsafeDB) -> list[bytes]:
    """Drain incoming blobs ready for delivery."""

    # Read from SQLite queue
    rows = unsafedb.query(
        """SELECT id, blob FROM incoming_blobs
           WHERE deliver_at <= ? AND NOT dropped
           ORDER BY deliver_at LIMIT ?""",
        (current_time_ms, batch_size)
    )

    if not rows:
        return []

    # Delete the rows we're returning
    ids = [row['id'] for row in rows]
    unsafedb.execute(
        f"DELETE FROM incoming_blobs WHERE id IN ({','.join('?' * len(ids))})",
        ids
    )

    return [row['blob'] for row in rows]
```

#### Phase 3: Real networking - thread-safe packet handoff

**Problem**: UDP recv runs in background thread. SQLite connections aren't thread-safe.

**Solution**: Use Python's thread-safe `queue.Queue` to pass packets from recv thread to main thread. Main thread INSERTs into SQLite.

**File: `tests/networking_tests/conftest.py`**

```python
@dataclass
class Client:
    # ... existing fields ...

    # Thread-safe buffer for UDP packets (recv thread -> main thread)
    _udp_buffer: queue.Queue = field(default_factory=queue.Queue, repr=False)

# In UDPSocket._recv_loop (background thread):
def _recv_loop(self):
    """Background thread: receive packets into thread-safe buffer."""
    while self._running:
        try:
            data, addr = self._sock.recvfrom(65535)
            # Don't touch SQLite here! Just buffer the packet.
            self._incoming.put((data, addr))
        except socket.timeout:
            continue

# In Client.tick (main thread):
def tick(self, t_ms: int):
    """Run a tick for this client."""
    from core.db import create_unsafe_db
    from core import tick as tick_module

    # Step 1: Move packets from thread-safe buffer to SQLite (main thread only)
    unsafedb = create_unsafe_db(self.db)
    packets = self.network.drain()  # Drains from thread-safe queue
    for data, addr in packets:
        unsafedb.execute(
            "INSERT INTO incoming_blobs (blob, sent_at, deliver_at) VALUES (?, ?, ?)",
            (data, t_ms, t_ms)  # deliver_at = now (immediate delivery)
        )

    # Step 2: Run tick - drain() reads from SQLite
    tick_module.tick(t_ms=t_ms, db=self.db)
    self.db.commit()
```

### Files Modified

| File | Change |
|------|--------|
| `core/queues.py` | add() uses simulator for physics then INSERTs; drain() reads from SQLite |
| `simulator/nspy_network.py` | New `calculate_delivery()` method (stateless) |
| `tests/networking_tests/conftest.py` | UDP packets go through thread-safe buffer, main thread INSERTs |

### Performance Optimization

The goal is maximum SQLite performance while keeping simulation and real networking isomorphic. We're already SQLite-bottlenecked for validation and projection - the queue shouldn't add significant overhead.

#### 1. Index on deliver_at

The drain query filters by `deliver_at <= ?`. Add an index:

```sql
CREATE INDEX IF NOT EXISTS idx_incoming_blobs_deliver_at ON incoming_blobs(deliver_at) WHERE NOT dropped;
```

Partial index excludes dropped packets (which we never query).

#### 2. DELETE ... RETURNING (SQLite 3.35+)

Avoid separate SELECT then DELETE. Use atomic DELETE with RETURNING:

```python
def drain(batch_size: int, current_time_ms: int, unsafedb: UnsafeDB) -> list[bytes]:
    """Drain incoming blobs - single atomic operation."""
    rows = unsafedb.execute_returning(
        """DELETE FROM incoming_blobs
           WHERE id IN (
               SELECT id FROM incoming_blobs
               WHERE deliver_at <= ? AND NOT dropped
               ORDER BY deliver_at
               LIMIT ?
           )
           RETURNING blob""",
        (current_time_ms, batch_size)
    )
    return [row['blob'] for row in rows]
```

This is atomic and ~2x faster than SELECT + DELETE.

#### 3. Batch INSERTs

For real networking, multiple packets may arrive between ticks. Batch them:

```python
def _insert_packets(self, packets: list[tuple[bytes, int]], unsafedb):
    """Batch insert packets - single transaction."""
    unsafedb.executemany(
        "INSERT INTO incoming_blobs (blob, sent_at, deliver_at) VALUES (?, ?, ?)",
        [(blob, t_ms, t_ms) for blob, t_ms in packets]
    )
```

`executemany` is significantly faster than individual INSERTs.

#### 4. Prepared Statement Reuse

The same INSERT/DELETE queries run every tick. SQLite caches prepared statements by query string, so use consistent query strings (no dynamic formatting).

#### 5. WAL Mode Tuning

Already using WAL mode. Additional tuning:

```sql
PRAGMA synchronous = NORMAL;      -- Faster than FULL, still safe with WAL
PRAGMA wal_autocheckpoint = 1000; -- Checkpoint every 1000 pages
PRAGMA mmap_size = 268435456;     -- 256MB memory-mapped I/O
```

#### 6. Transaction Boundaries

The drain + process cycle should be in a single transaction:
1. DELETE RETURNING (drain packets)
2. Process packets (unwrap_and_store for each)
3. COMMIT

Don't commit between drain and process - if we crash, packets are lost but that's OK (network is unreliable anyway).

#### 7. Queue Depth Monitoring and Bankruptcy

Use **time-based** bankruptcy to ensure we're never more than a few seconds behind:

```python
MAX_QUEUE_AGE_MS = 3000  # Never more than 3 seconds behind

def drain(batch_size: int, current_time_ms: int, unsafedb: UnsafeDB) -> list[bytes]:
    """Drain incoming blobs, with time-based bankruptcy protection."""

    # Drop packets older than MAX_QUEUE_AGE_MS
    # This ensures recent messages are processed promptly even during bursts
    cutoff_time = current_time_ms - MAX_QUEUE_AGE_MS
    result = unsafedb.execute(
        "DELETE FROM incoming_blobs WHERE deliver_at < ? AND NOT dropped",
        (cutoff_time,)
    )
    if result.rowcount > 0:
        log.warning(f"Queue bankruptcy: dropped {result.rowcount} packets older than {MAX_QUEUE_AGE_MS}ms")

    # Normal drain - only packets within our time window
    rows = unsafedb.execute_returning(
        """DELETE FROM incoming_blobs
           WHERE id IN (
               SELECT id FROM incoming_blobs
               WHERE deliver_at <= ? AND NOT dropped
               ORDER BY deliver_at
               LIMIT ?
           )
           RETURNING blob""",
        (current_time_ms, batch_size)
    )
    return [row['blob'] for row in rows]
```

**Why time-based**:
- Count-based (10K packets) could still mean minutes of latency
- Time-based guarantees max latency of `MAX_QUEUE_AGE_MS`
- A new message is always processed within a few seconds
- Old backlog doesn't delay recent messages

**Why bankruptcy is OK**:
- Network is unreliable anyway - packets can always be lost
- Negentropy sync will recover any missed events eventually
- Responsiveness matters more than completeness
- Users expect messages within seconds, not minutes

#### 8. Packet Size Assumptions

Current transit blob sizes:
- Sync/negentropy messages: 100-500 bytes
- Individual events: 500 bytes - 2 KB (some larger)
- File slice events: can be larger (chunked file data)

SQLite stores BLOBs inline up to ~4KB, larger ones overflow to separate pages. Our typical sizes fit inline, which is efficient.

**Future**: Events will be clamped to ~500 bytes to fit safely within UDP MTU limits (~1400 bytes with headers). This will make queue operations even more efficient. For now, some events may exceed 1KB but this doesn't affect the queue design.

#### 9. Benchmark Target

**Target**: Queue operations (INSERT batch + DELETE RETURNING) should add <1ms per tick for typical batch sizes (10-100 packets).

**Validation**: Add a simple benchmark that measures:
- INSERT 100 packets
- Advance time
- DELETE RETURNING 100 packets
- Assert total < 5ms on typical hardware

### Tradeoffs and Justification

#### SQLite vs In-Memory

**Concern**: SQLite operations vs simple list append/pop.

**Why it's acceptable**:
- We're already SQLite-bottlenecked for event validation and projection
- Queue overhead is small relative to event processing
- Keeping simulation/real-network isomorphic is worth the overhead
- The optimizations above minimize the gap

#### Thread Safety (UDP recv thread vs main thread)

**Concern**: SQLite connections aren't thread-safe. UDP recv thread can't share connection with main thread.

**Solution**: UDP recv thread never touches SQLite. It puts packets into a Python `queue.Queue` (thread-safe). The main thread drains this buffer and INSERTs into SQLite at the start of each tick.

This is clean separation:
- Background thread: network I/O only
- Main thread: all SQLite operations

#### Loss of SimPy Discrete-Event Simulation

**Concern**: Current simulator uses SimPy infrastructure. We'd be discarding it.

**Why it's OK**: We're not using SimPy's power. The simulator just calculates `deliver_at = t_ms + latency`. Checking `deliver_at <= current_t_ms` in SQL is equivalent and simpler. No precision is lost.

#### Loss/NAT/Partition Modeling

**Concern**: These features must keep working.

**Why it's OK**: They're orthogonal to storage backend. The simulator's `calculate_delivery()` method still applies all the physics:
- Loss model → returns `should_drop=True`
- NAT engine → checks mappings, returns `should_drop=True` if blocked
- Partitions → returns `should_drop=True` for partitioned peers

Same logic, just doesn't store packets itself.

### Migration

Scenario tests continue working unchanged - `queues.incoming.add()` uses simulator for physics and writes to SQLite. No API changes needed for existing test code.

---

## Part 2: Server Mode (Future Work)

### Concept

For production deployment, some peers run as **always-on servers** (VPS, cloud). These differ from clients:

| Aspect | Client | Server |
|--------|--------|--------|
| Transport | UDP (may be behind NAT) | QUIC/WebSocket (public IP, firewall-friendly) |
| Connections | Initiates outbound | Accepts inbound |
| Invite flow | Clicks/pastes invite link | Receives invite via HTTP endpoint |

### Why QUIC/WebSocket?

- **Firewall-friendly** - Looks like normal HTTPS traffic
- **Works through corporate proxies** - Unlike raw UDP
- **TLS built-in** - QUIC has TLS 1.3, WebSocket uses WSS

### Server Invite Flow

Servers can't click invite links, so they expose an HTTP endpoint:

```
Client                              Server
  │                                    │
  │   POST /submit-invite              │
  │   { "invite_link": "quiet://..." } │
  │─────────────────────────────────►  │
  │                                    │
  │   Server joins network,            │
  │   publishes peer_address event     │
  │                                    │
  │◄──────── QUIC/WS sync ────────────►│
```

**Production considerations** (not needed for initial implementation):
- Auth (API key, OAuth) or CAPTCHA on invite endpoint
- Rate limiting
- TLS certificates

### CLI Flag (Placeholder)

```bash
# Future: run in server mode
python cli.py --db server.db --server --port 443
```

**Implementation deferred** - Focus on UDP-based real networking tests first. Server mode can reuse the same packet processing pattern (call `sync.unwrap_and_store()` directly on received packets).

---

## Verification

### Part 1: Real Networking Isolation

1. Run existing simulated tests - should pass unchanged:
   ```bash
   PYTHONPATH=. pytest tests/scenario_tests/ -v --tb=short -x
   ```

2. Run networking tests - should pass with isolation:
   ```bash
   PYTHONPATH=. pytest tests/networking_tests/ -v --tb=short
   ```

3. Verify no cross-client interference:
   - Alice's packets don't appear in Bob's processing
   - Each client only processes its own incoming packets

---

## Summary

| Component | Change | Complexity |
|-----------|--------|------------|
| Unified packet queue | `add()` INSERTs to SQLite, `drain()` SELECTs from SQLite | Core change |
| Stateless simulator | New `calculate_delivery()` method, remove in-memory queue | Refactor |
| Real networking tests | UDP recv → thread-safe buffer → main thread INSERTs | Test infra |
| Server mode | Future work | Deferred |

**Key insight**: One queue (`incoming_blobs` table), multiple sources. The simulator becomes a stateless physics calculator. Each client's database provides automatic isolation. No special code paths for "simulated" vs "real" - just different sources writing to the same SQLite queue.
