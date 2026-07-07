# Real P2P Networking Plan for Quiet Protocol CLI

## Overview

Add real UDP-based peer-to-peer networking so two CLI instances can connect over localhost at different ports and sync events in real-time, while preserving the simplicity and testability of the current tick-based architecture.

## Design Philosophy

**Keep what works:**
- tick() remains the core processing loop
- SQLite queues continue to work (for testing, offline buffering)
- All existing sync/projection logic unchanged
- Deterministic, testable, debuggable

**Add real networking as a thin layer:**
- UDP socket receives packets → puts in queue → triggers tick
- UDP socket sends packets when sync needs to transmit
- Debounce rapid packet arrivals to avoid tick storms
- No connection state machine, no handshake complexity

## Current Architecture (Tick + SQLite Queues)

```
CLI A                           CLI B
  │                               │
  ├─► tick()                      ├─► tick()
  │     │                         │     │
  │     ├─► sync.send_request()   │     ├─► sync.send_request()
  │     │     │                   │     │     │
  │     │     └─► queues.incoming.put()  (simulated)
  │     │                         │     │
  │     └─► queues.incoming.drain()     │
  │           │                   │     │
  │           └─► recorded.project()    │
  │                               │
  └─► (repeat on timer)           └─► (repeat on timer)
```

## Proposed Architecture (Reactive Tick + UDP)

```
CLI A (port 9001)                          CLI B (port 9002)
  │                                          │
  ├─► UDP Socket (recv on 9001)              ├─► UDP Socket (recv on 9002)
  │     │                                    │     │
  │     └─► on_packet(data, addr)            │     └─► on_packet(data, addr)
  │           │                              │           │
  │           ├─► queues.incoming.put()      │           ├─► queues.incoming.put()
  │           │                              │           │
  │           └─► schedule_tick(debounce=50ms)           └─► schedule_tick(debounce=50ms)
  │                 │                        │
  │                 └─► tick()  ◄────────────┼─── (same tick() as before!)
  │                       │                  │
  │                       ├─► drain queue    │
  │                       ├─► project events │
  │                       └─► sync.send()────┼──► UDP sendto(9002)
  │                                          │
  └─► CLI commands trigger tick() directly   └─► CLI commands trigger tick() directly
```

**Key insight**: We don't need an async event loop. We need:
1. A thread that receives UDP packets and queues them
2. A debounced trigger that calls existing tick()
3. A way for tick() to send UDP packets instead of simulated queue

## Design Decisions

### 1. Transport Layer: UDP (Connectionless)

**Why UDP**:
- Simpler connection model (we have connection events but no TCP state machine)
- Fire-and-forget sending matches our event model
- Existing sync protocol handles retransmission (negentropy detects missing events)
- DAG-based causality handles ordering (not TCP's byte-stream ordering)
- Simpler to implement and test
- Natural fit for eventual consistency model

**Note**: We do have connection events (`connection.py`) but they're simpler than TCP -
just req/ack for key exchange, not TCP's SYN/SYN-ACK/ACK state machine.

**Trade-offs accepted**:
- No guaranteed delivery → sync protocol re-requests missing events
- No ordering → DAG dependencies provide causal ordering
- Size limit (~1400 bytes safe MTU) → will address separately with chunking
- No flow control → debounce on receive side

**Packet Format** (simple, defer wire format to separate project):
```
┌─────────────────────────────────────────┐
│ Transit blob (existing format)          │
│ Already includes: encryption, signature │
│ Already self-describing via event type  │
└─────────────────────────────────────────┘
```

### 2. Threading Model: Receiver Thread + Main Thread

**Why not asyncio**:
- Adds complexity for little benefit in our case
- CLI uses readline which doesn't play nice with asyncio
- tick() is synchronous and works fine
- Easier to reason about and debug

**Structure**:
```python
class UDPNetwork:
    def __init__(self, port: int):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', port))
        self.incoming = queue.Queue()  # Thread-safe
        self.peers = {}  # peer_id -> (host, port)
        self._tick_scheduled = False
        self._tick_timer = None

    def start_receiver(self):
        """Background thread: recv packets, queue them, schedule tick"""
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def _recv_loop(self):
        while True:
            data, addr = self.sock.recvfrom(65535)
            self.incoming.put((data, addr))
            self._schedule_tick_debounced()

    def _schedule_tick_debounced(self, delay_ms=50):
        """Debounce: wait for packet storm to settle before tick"""
        if self._tick_timer:
            self._tick_timer.cancel()
        self._tick_timer = threading.Timer(delay_ms/1000, self._trigger_tick)
        self._tick_timer.start()

    def send(self, peer_id: str, data: bytes):
        """Send packet to known peer"""
        if peer_id in self.peers:
            addr = self.peers[peer_id]
            self.sock.sendto(data, addr)
```

### 3. Peer Registry (Minimal)

**In-memory address book**:
```python
@dataclass
class KnownPeer:
    peer_id: str
    address: tuple[str, int]  # (host, port)
    last_seen: float  # For debugging/display

# Populated via:
# 1. Manual: `connect <peer_id> 127.0.0.1:9002`
# 2. Invite: address embedded in invite link
# 3. Observed: learn address when we receive packet from peer
```

**No handshake needed**:
- Transit blobs already include sender peer_id (signed)
- We verify signature → we know who sent it
- We remember their address from the packet source
- Authentication happens at event layer, not transport layer

### 4. Integration with Existing Code

**Minimal changes - just wire up the plumbing**:

1. **`core/net.py`** (NEW - ~100 lines)
   ```python
   class UDPNetwork:
       def __init__(self, port): ...
       def start(self): ...
       def send(self, peer_id, blob): ...
       def drain_incoming(self) -> list[bytes]: ...
   ```

2. **`core/queues.py`** - Add network as optional transport
   ```python
   class IncomingQueue:
       def __init__(self, db, network: UDPNetwork = None):
           self.db = db
           self.network = network

       def drain(self):
           # First drain from network (if present)
           if self.network:
               for blob, addr in self.network.drain_incoming():
                   yield blob
           # Then drain from SQLite (offline buffer, tests)
           yield from self._drain_sqlite()
   ```

3. **`events/network/sync.py`** - Add send callback
   ```python
   def send_blob(db, peer_id, target_peer_id, blob, *, send_fn=None):
       if send_fn:
           send_fn(target_peer_id, blob)  # Real network
       else:
           queues.incoming.put(db, target_peer_id, blob)  # Simulated
   ```

4. **`cli.py`** - Wire up network on startup
   ```python
   if args.port:
       network = UDPNetwork(args.port)
       network.start()
       # tick() now drains from network
   ```

### 5. Debounce Strategy (Fixed: Leading-Edge + Max-Wait)

**Goal**: Reactivity without tick storms, but guaranteed progress

**Problem with trailing-edge-only debounce**:
```
Packets arrive:     ─●──●●●──●──●──●──●──●──●──►  (continuous)
                     │  │││  │  │  │  │  │  │
Trailing debounce:  [==50ms==][==50ms==][==50ms  (never fires!)
```

**Solution: Leading-edge trigger + max-wait cap**:
```
Packets arrive:     ─●──●●●──●──●──●──●──●──●──►
                     │
Leading edge:        ●  (fire immediately on first packet)
                     │
                    [====== max 100ms wait ======]
                                                 │
Max-wait fires:                                  ●  (guaranteed progress)
```

**Implementation**:
```python
class TickScheduler:
    def __init__(self, tick_fn, min_interval_ms=10, max_wait_ms=100):
        self.tick_fn = tick_fn
        self.min_interval = min_interval_ms / 1000
        self.max_wait = max_wait_ms / 1000
        self._tick_pending = threading.Event()
        self._first_packet_time = None
        self._last_tick_time = 0
        self._lock = threading.Lock()

    def notify_packet(self):
        """Called by receiver thread when packet arrives"""
        with self._lock:
            now = time.time()
            if self._first_packet_time is None:
                self._first_packet_time = now
                # Leading edge: signal immediately
                self._tick_pending.set()
            elif now - self._first_packet_time >= self.max_wait:
                # Max wait exceeded: force tick
                self._tick_pending.set()
            # Otherwise: coalesce (trailing edge handled by max_wait)

    def wait_for_tick(self, timeout=None):
        """Called by main thread to wait for tick signal"""
        return self._tick_pending.wait(timeout)

    def run_tick(self):
        """Called by main thread to execute tick"""
        with self._lock:
            self._tick_pending.clear()
            self._first_packet_time = None
            self._last_tick_time = time.time()
        self.tick_fn()
```

**Thread safety**:
- Receiver thread only sets event, never calls tick()
- Main thread owns tick() execution
- Lock protects shared state, but is never held during tick()

### 6. Main Thread Tick Execution

**Critical**: tick() must run on main thread because:
1. SQLite connection isn't thread-safe by default
2. CLI state (prompts, output) lives on main thread
3. Avoids all concurrency bugs in existing code

**REPL integration** (preserves CLI simplicity):
```python
def repl_with_network(db, peer_id, network):
    scheduler = TickScheduler(lambda: tick(db, peer_id))
    network.set_scheduler(scheduler)
    network.start()

    while True:
        # Wait for either: user input OR tick signal (100ms timeout)
        ready, _, _ = select.select([sys.stdin], [], [], 0.1)

        # Check if tick needed (non-blocking)
        if scheduler.wait_for_tick(timeout=0):
            scheduler.run_tick()

        # Process user input if available
        if ready:
            line = sys.stdin.readline()
            if not line:
                break
            handle_command(db, peer_id, line.strip())
            # User input also triggers immediate tick
            tick(db, peer_id)
```

**Fallback periodic tick** (handles last-packet-dropped):
```python
# Every 1 second, do a tick regardless (catches stragglers)
PERIODIC_TICK_INTERVAL = 1.0

if time.time() - last_periodic_tick > PERIODIC_TICK_INTERVAL:
    tick(db, peer_id)
    last_periodic_tick = time.time()
```

### 7. Non-Interactive Mode (for LLMs/Scripts)

**Problem**: When an LLM runs the CLI non-interactively, it issues commands and expects results.
But we can't know "converged" from one client - we don't know what the other side has.

**Solution**: Condition-based waiting (inspired by `assert_eventually` in tests)

The LLM specifies WHAT it's waiting for, not "convergence":

```bash
# Wait until I can see Bob's message
python cli.py --db alice.db -c "sync-until 'messages | grep hello'" --timeout 5s

# Wait until specific message appears
python cli.py --db alice.db -c "sync-until-sql 'SELECT 1 FROM messages WHERE content LIKE \"%hello%\"'" --timeout 5s

# Simple: just tick N times
python cli.py --db alice.db -c "sync-ticks 20"
```

**Implementation** (modeled on `assert_eventually`):
```python
def sync_until(db, peer_id, network, check_fn, timeout_s=5.0, interval_s=0.1):
    """Run ticks until check_fn() returns truthy or timeout.

    Like assert_eventually but for CLI use.
    """
    start = time.time()

    while time.time() - start < timeout_s:
        tick(db, peer_id)

        try:
            result = check_fn()
            if result:
                return True
        except Exception:
            pass  # Not ready yet, keep ticking

        time.sleep(interval_s)

    return False  # Timeout

# CLI wrapper for shell command check
def sync_until_cmd(db, peer_id, network, shell_cmd, timeout_s=5.0):
    """Tick until shell command succeeds (exit 0)"""
    def check():
        result = subprocess.run(shell_cmd, shell=True, capture_output=True)
        return result.returncode == 0
    return sync_until(db, peer_id, network, check, timeout_s)

# CLI wrapper for SQL check
def sync_until_sql(db, peer_id, network, sql, timeout_s=5.0):
    """Tick until SQL query returns a row"""
    def check():
        return db.query_one(sql) is not None
    return sync_until(db, peer_id, network, check, timeout_s)
```

**LLM usage pattern**:
```bash
# Send message, then wait until we see a reply
python cli.py --db alice.db \
    -c "send 'hello bob'" \
    -c "sync-until-sql \"SELECT 1 FROM messages WHERE author != 'alice'\" --timeout 10s"

# Or just tick a bunch of times and check after
python cli.py --db alice.db -c "send 'hello'" -c "sync-ticks 50"
python cli.py --db alice.db -c "messages"
```

### 8. Address Discovery & peer_address Events

**Design**: Addresses are shared via `peer_address` events that project to shared state.
This means invite links can include address info, and peers learn each other's addresses
through the event system (not just from packet source).

**peer_address event**:
```python
# New event type
{
    "type": "peer_address",
    "peer_id": "abc123...",
    "host": "127.0.0.1",
    "port": 9001,
    "transport": "udp",  # future: "tcp", "quic"
    # Standard fields: network_id, created_at, signature, etc.
}

# Projects to:
# peer_addresses(peer_id, host, port, transport, recorded_by, ...)
```

**Invite links include address**:
```
quiet://net123?invite=xyz&addr=127.0.0.1:9001

# CLI parses this and:
# 1. Joins the network (existing invite flow)
# 2. Creates peer_address event for the inviter
# 3. Knows where to send first sync request
```

**For localhost testing** (Phase 1):
- Invite link includes address (parsed by CLI, creates peer_address event)
- Manual: `peer-add <peer_id> 127.0.0.1:9002` (creates peer_address event)
- Auto-learn: when we receive a valid signed packet, create peer_address event

**Zeroconf/mDNS for LAN** (Phase 2):
Simple LAN discovery using `zeroconf` Python package (pip install zeroconf):

```python
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

SERVICE_TYPE = "_quietproto._udp.local."

# Register ourselves when we listen
def register_service(port, peer_id, network_id):
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{peer_id}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton("0.0.0.0")],
        port=port,
        properties={"network_id": network_id, "peer_id": peer_id},
    )
    zeroconf = Zeroconf()
    zeroconf.register_service(info)
    return zeroconf

# Discover peers on LAN
class PeerListener:
    def add_service(self, zc, type_, name):
        info = zc.get_service_info(type_, name)
        if info and info.properties.get("network_id") == our_network_id:
            # Found a peer! Create peer_address event
            host = socket.inet_ntoa(info.addresses[0])
            port = info.port
            peer_id = info.properties["peer_id"]
            # ... create peer_address event ...

zeroconf = Zeroconf()
browser = ServiceBrowser(zeroconf, SERVICE_TYPE, PeerListener())
```

This is NOT secure (anyone on LAN can see/spoof) but fine for testing.
Production would use signed announcements or DHT.

**Future** (Phase 3+):
- DHT for internet-wide discovery
- UDP hole punching for NAT traversal
- Port forwarding detection/UPnP

### Helper Servers (QUIC/WebSocket)

For production use, clients should be able to connect to helper servers that relay
traffic. These should use QUIC or WebSocket to avoid being flagged as "exotic" traffic
by institutional networks (firewalls, corporate proxies, etc.).

**Why QUIC/WebSocket for helper servers**:
- Looks like normal HTTPS traffic on the wire
- Works through corporate firewalls and proxies
- TLS built-in (QUIC) or easy to add (WSS)
- Widely supported, not flagged as suspicious

**Architecture**:
```
Direct P2P (when possible):    Client A ←──UDP──→ Client B

Via Helper Server:             Client A ←─QUIC/WS─→ Helper ←─QUIC/WS─→ Client B
```

**Helper server responsibilities**:
- Accept WebSocket/QUIC connections from clients
- Relay transit blobs between connected clients
- Optional: Store-and-forward for offline clients
- Optional: NAT traversal assistance (STUN/TURN-like)

**Implementation** (Phase 4+):
- Python: `aioquic` for QUIC, `websockets` for WebSocket
- Helper server is just a relay - no decryption, no event parsing
- Detailed proposal: `docs/planning/websocket-quic-transport-proposal.md`
- Clients treat helper server as another "transport", events flow same way

### 9. New CLI Commands

```
listen <port>                     # Bind UDP socket to port, start receiving
peer-add <peer_id> <host:port>    # Add peer address (creates peer_address event)
peers                             # List known peers and their addresses
sync-ticks <n>                    # Run n ticks (simple, predictable)
sync-until <shell_cmd>            # Tick until shell command exits 0
sync-until-sql <query>            # Tick until SQL returns a row
```

**Updated invite command** (generates copyable link with address):
```
quiet> invite bob
Invite link (copy this):
quiet://abc123?invite=xyz789&addr=127.0.0.1:9001

# The addr= is our listen address so Bob knows where to connect
```

### 10. File Structure

```
core/
├── net.py              # UDPNetwork class (~100 lines)
├── tick_scheduler.py   # TickScheduler class (~50 lines)
└── ... (existing files unchanged)
```

## Implementation Phases

### Phase 1: Basic UDP Transport (MVP)
- [ ] UDPNetwork class with recv thread
- [ ] TickScheduler with leading-edge + max-wait
- [ ] Wire into REPL loop (select-based)
- [ ] `listen`, `peer-add`, `peers` commands
- [ ] Auto-learn peer addresses from incoming packets
- [ ] Existing tick/sync logic unchanged

### Phase 2: Non-Interactive & LLM Support
- [ ] `sync-ticks`, `sync-until`, `sync-until-sql` commands
- [ ] Test with LLM-style scripted usage
- [ ] peer_address event type and projection

### Phase 3: Zeroconf LAN Discovery
- [ ] `pip install zeroconf` dependency
- [ ] Service registration on `listen`
- [ ] Peer discovery via mDNS
- [ ] Auto-create peer_address events for discovered peers

### Phase 4: Robustness
- [ ] Periodic tick fallback (1s interval)
- [ ] Peer address validation (only accept from known network members)
- [ ] Rate limiting on receive side
- [ ] Graceful handling of oversized packets
- [ ] Socket cleanup on exit

### Phase 5: NAT Traversal (Future)
- [ ] UDP hole punching
- [ ] UPnP port forwarding detection
- [ ] STUN for public IP discovery

## Example Session

```
# Terminal 1 - Alice creates network and listens
$ python cli.py --db alice.db
quiet> new-network "test"
Created network abc123, peer alice_peer
quiet> listen 9001
Listening on UDP 127.0.0.1:9001
quiet> invite bob
Invite link (copy this):
quiet://abc123?invite=xyz789&addr=127.0.0.1:9001

# Terminal 2 - Bob joins using the link (includes Alice's address!)
$ python cli.py --db bob.db
quiet> accept-invite quiet://abc123?invite=xyz789&addr=127.0.0.1:9001
Joined network abc123
Created peer_address for alice_peer at 127.0.0.1:9001
quiet> listen 9002
Listening on UDP 127.0.0.1:9002
quiet> send "Hello Alice!"
(tick runs, syncs with Alice)

# Terminal 1 - Alice sees message (within ~100ms)
[bob] Hello Alice!
quiet> send "Hi Bob!"

# Terminal 2 - Bob sees reply (within ~100ms)
[alice] Hi Bob!
```

**Non-interactive (LLM) usage**:
```bash
# Alice sends, then waits until she sees a reply from someone else
$ python cli.py --db alice.db --port 9001 \
    -c "send 'hello'" \
    -c "sync-until-sql \"SELECT 1 FROM messages WHERE author_user_id != (SELECT user_id FROM local_peers LIMIT 1)\" --timeout 10"

# Or simpler: just tick a bunch and check
$ python cli.py --db bob.db --port 9002 -c "sync-ticks 50" -c "messages"
[alice] hello
```

## Comparison with BitTorrent Architecture

| Aspect | BitTorrent | Our Design |
|--------|------------|------------|
| Transport | TCP + uTP | UDP (connectionless) |
| Framing | 4-byte length prefix | Transit blob (self-describing) |
| Handshake | info_hash + peer_id | Connection events (simpler) |
| Messages | have, piece, request... | event, sync_request... |
| Discovery | DHT, trackers, PEX | Invite links, zeroconf |
| State | Piece bitmap | Event set (negentropy) |
| Sync | Piece requests | Negentropy set reconciliation |
| Reliability | TCP guarantees | DAG deps + negentropy resync |

## Open Questions

1. **Should we keep SQLite queue as fallback?** - Yes, useful for offline buffering and deterministic testing

2. **Max packet size before chunking?** - Defer to separate wire format project; for now reject >64KB

3. **Peer validation timing?** - When to update peer address: immediately on valid signature, or after membership check?

4. **Rate limiting strategy?** - Per-peer? Per-network? What thresholds?

## Dependencies

- Python 3.10+ (for match statements, improved typing)
- No new external dependencies (socket, threading, select are stdlib)

## Lessons from Real P2P Systems

### From libtorrent (High-Performance C++ BitTorrent)

1. **Single Event Loop, Multiple Torrents**: libtorrent serves all torrents on one port in one thread using boost.asio. Messages on one peer connection may trigger responses on other connections.

2. **Socket Corking**: Buffer outgoing messages and flush together. "The connection is added to an uncork-set of peer connections. libtorrent uncorks all peers once the message queue is drained." Fewer syscalls = better throughput.

3. **Async Disk I/O**: All disk operations are async to the network thread. Network never waits on disk.

4. **Adaptive Batching**: "Always complete all work queued up for a thread before going back to sleep." Batch sizes grow organically with load.

5. **Message Queue Architecture**: Each API call posts to a message queue processed by the network thread. No direct data structure access across threads.

### From cratetorrent (Rust BitTorrent)

1. **Task-per-Peer**: Each peer session runs on a separate Tokio task. The peer's `run()` method multiplexes: incoming messages, disk writes, torrent updates.

2. **Two-Tiered Channels**: Global alert channels for torrent-level events, per-torrent channels for specific operations (block writes, piece completions).

3. **Don't Await Disk**: When writing blocks, send command to disk task and continue. Receive result later via alert channel.

4. **Bandwidth-Delay Product**: Dynamically adjust request queue size based on measured throughput. "Q = B * D / 16 KiB"

5. **Weighted RTT Averages**: Track per-peer round-trip times with weighted running averages. Timeout thresholds adapt per-peer.

### Key Patterns to Adopt

```
┌─────────────────────────────────────────────────────────────┐
│  Pattern                      │  Our Application            │
├───────────────────────────────┼─────────────────────────────┤
│  Adaptive batching            │  Drain all queued per tick  │
│  Leading-edge + max-wait      │  Responsive but no starvation│
│  Main-thread execution        │  tick() always on main      │
│  Periodic fallback            │  1s tick catches stragglers │
│  Event-based wake             │  threading.Event for signal │
└───────────────────────────────┴─────────────────────────────┘
```

### What NOT to Do (Anti-Patterns)

1. **Trailing-edge-only debounce** - Can starve tick() during traffic
2. **tick() from background thread** - SQLite and CLI aren't thread-safe
3. **Complex async machinery** - Adds bugs, doesn't add value here
4. **Magic number batch sizes** - Let queue depth determine batch

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Debounce starvation | Leading-edge + max-wait guarantees progress |
| Thread safety | tick() only runs on main thread |
| Breaking existing tests | Keep SQLite queue path, network is additive |
| Port conflicts | Clear error messages, suggest alternative ports |
| UDP packet loss | Existing bloom sync handles missing events |
| Large packets dropped | Reject >64KB, defer chunking to wire format project |
