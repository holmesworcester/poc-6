# ns.py Network Simulator Integration Plan

## Overview

Replace the custom network simulator (`network_config.py`, `queues.incoming`) with `ns.py`, a discrete-event network simulator built on SimPy. This addresses several issues identified in the current implementation:

### Problems with Current Simulator

1. **Bandwidth limiting uses hard 1-second window reset** - allows burst behavior at window boundaries
2. **SQLite overhead per packet** - INSERT + SELECT + DELETE for every packet
3. **Global mutable state** - `_config`, `_burst_loss_remaining`, etc. cause test interference
4. **Independent packet loss** - no correlation, unrealistic for real networks
5. **No proper queueing model** - no buffer overflow, no queue-based delay

### Benefits of ns.py

1. **Token bucket shaping** - proper sustained rate limiting
2. **In-memory operation** - no database overhead
3. **Queue-based delays** - realistic buffer behavior with tail-drop
4. **Pluggable distributions** - latency, loss, jitter via callables
5. **Instance-based state** - no global state pollution

---

## Architecture

### Current Flow

```
send_packet() → queues.incoming.add() → SQLite INSERT
                                     ↓
tick() → queues.incoming.drain() → SQLite SELECT + DELETE → process blobs
```

### Proposed Flow

```
send_packet() → NetworkSimulator.send() → Wire/Port/Shaper pipeline
                                       ↓
tick() → NetworkSimulator.advance_to(t_ms) → SimPy env.run() → delivered packets
```

---

## Component Mapping

| Current | ns.py Equivalent | Notes |
|---------|------------------|-------|
| `network_config.latency_ms` | `Wire(delay_dist=...)` | Function returning delay |
| `network_config.jitter_ms` | `Wire(delay_dist=lambda: gauss(...))` | Distribution function |
| `network_config.packet_loss_rate` | `Wire(loss_dist=lambda: rate)` | Loss probability function |
| `network_config.bandwidth_bytes_per_sec` | `TokenBucketShaper(rate, bucket_size)` | Proper token bucket |
| `network_config.max_packet_size` | Custom check before `Wire.put()` | Pre-filter |
| `network_config.burst_loss_*` | Custom `loss_dist` with state | Gilbert-Elliott model |
| `network_config.partitioned_peers` | Check before routing | Pre-filter |
| NAT simulation | Keep existing `simulator/nat.py` | ns.py doesn't have NAT |

---

## API Design

### New NetworkSimulator Class

```python
class NetworkSimulator:
    """SimPy-based network simulator with realistic network conditions."""

    def __init__(self):
        self.env = simpy.Environment()
        self.config = NetworkConfig()  # Immutable config
        self.nat_engine = NatEngine()  # Keep existing NAT
        self.delivered = []  # Packets ready for processing
        self._links: dict[tuple[str, str], Link] = {}  # Per-peer-pair links

    def configure(self, config: NetworkConfig) -> None:
        """Update network configuration (rebuilds links)."""

    def register_peer(self, peer_id: str, behind_nat: bool = False, ...) -> None:
        """Register peer with NAT engine."""

    def send(self, from_peer: str, to_peer: str, blob: bytes, t_ms: int) -> bool:
        """Send packet through simulated network. Returns False if dropped immediately."""

    def advance_to(self, t_ms: int) -> None:
        """Advance simulation time, delivering ready packets."""

    def drain(self, max_count: int = None) -> list[bytes]:
        """Get delivered packets (removes from queue)."""

    def partition_peer(self, peer_id: str) -> None:
    def unpartition_peer(self, peer_id: str) -> None:

    def reset(self) -> None:
        """Reset to initial state."""
```

### NetworkConfig (Immutable)

```python
@dataclass(frozen=True)
class NetworkConfig:
    """Immutable network configuration."""
    latency_ms: float = 0.0
    jitter_ms: float = 0.0  # Standard deviation
    packet_loss_rate: float = 0.0  # 0.0 to 1.0
    bandwidth_bps: int | None = None  # bits/sec, None = unlimited
    bucket_size_bytes: int = 65536  # Token bucket size
    max_packet_size: int = 10000
    burst_loss_probability: float = 0.0
    burst_loss_length: int = 3
```

---

## Implementation Plan

### Phase 1: Core Simulator (This PR)

1. **Create `simulator/nspy_network.py`** with:
   - `NetworkSimulator` class wrapping SimPy environment
   - `Link` class composing Wire + optional TokenBucketShaper
   - `PacketCollector` sink that accumulates delivered packets

2. **Create `simulator/loss_models.py`** with:
   - `independent_loss(rate)` - simple random loss
   - `gilbert_elliott_loss(p_good_to_bad, p_bad_to_good, loss_in_bad)` - bursty loss

3. **Keep existing NAT** - `simulator/nat.py` unchanged

### Phase 2: Integration

4. **Create adapter in `queues.py`**:
   - New `incoming` class that delegates to NetworkSimulator
   - Same API: `add()`, `drain()`
   - Backward compatible with existing callers

5. **Update `simulator/network.py`**:
   - Use new NetworkSimulator internally
   - Same external API

### Phase 3: Migration

6. **Update tests** to use new simulator
7. **Remove old code** from `network_config.py` (keep NAT parts)
8. **Performance benchmarks** comparing old vs new

---

## Detailed Design: Link Pipeline

Each peer-to-peer link has this pipeline:

```
Packet → [SizeFilter] → [LossFilter] → [TokenBucket] → [Wire] → Collector
              ↓              ↓              ↓            ↓
           dropped        dropped        queued       delayed
```

### SizeFilter (Custom)
- Drops packets > max_packet_size
- Zero delay, synchronous check

### LossFilter (Custom)
- Applies packet loss model (independent or Gilbert-Elliott)
- Zero delay, synchronous check

### TokenBucketShaper (ns.py)
- Rate limits sustained throughput
- Queues packets when bucket empty
- `rate` in bits/sec, `bucket_size` in bytes

### Wire (ns.py)
- Adds propagation delay with distribution
- Optional additional loss (for link-layer loss)

### Collector (Custom)
- Accumulates delivered packets
- Packets tagged with delivery time

---

## Gilbert-Elliott Loss Model

For realistic bursty loss, implement two-state Markov model:

```python
class GilbertElliottLoss:
    """Two-state Markov loss model."""

    def __init__(self,
                 p_good_to_bad: float = 0.05,   # Transition: good → bad
                 p_bad_to_good: float = 0.3,    # Transition: bad → good
                 loss_in_good: float = 0.0,     # Loss rate in good state
                 loss_in_bad: float = 0.5):     # Loss rate in bad state
        self.state = 'good'
        ...

    def __call__(self, packet_id: int = None) -> float:
        """Returns loss probability for this packet."""
        # Transition states
        if self.state == 'good':
            if random.random() < self.p_good_to_bad:
                self.state = 'bad'
        else:
            if random.random() < self.p_bad_to_good:
                self.state = 'good'

        # Return loss probability for current state
        return self.loss_in_good if self.state == 'good' else self.loss_in_bad
```

---

## Time Synchronization

SimPy uses float time; our system uses int milliseconds.

```python
class NetworkSimulator:
    def __init__(self):
        self.env = simpy.Environment()
        self._time_scale = 1000.0  # SimPy units per ms

    def _to_simpy_time(self, t_ms: int) -> float:
        return t_ms / self._time_scale

    def _to_ms(self, simpy_time: float) -> int:
        return int(simpy_time * self._time_scale)

    def advance_to(self, t_ms: int) -> None:
        target = self._to_simpy_time(t_ms)
        if target > self.env.now:
            self.env.run(until=target)
```

---

## Migration Strategy

### Backward Compatibility

The new simulator exposes the same `queues.incoming.add()` / `drain()` API:

```python
# queues.py - adapter layer

_simulator: NetworkSimulator | None = None

def get_simulator() -> NetworkSimulator:
    global _simulator
    if _simulator is None:
        _simulator = NetworkSimulator()
    return _simulator

class incoming:
    @staticmethod
    def add(blob: bytes, t_ms: int, unsafedb: UnsafeDB,
            from_peer: str = None, to_peer: str = None) -> bool:
        # unsafedb ignored - no longer using SQLite for packet queue
        return get_simulator().send(from_peer, to_peer, blob, t_ms)

    @staticmethod
    def drain(batch_size: int, current_time_ms: int, unsafedb: UnsafeDB) -> list[bytes]:
        sim = get_simulator()
        sim.advance_to(current_time_ms)
        return sim.drain(batch_size)
```

### Config Migration

```python
# Old API (still works via adapter)
network_config.set_network_config(NetworkConfig(latency_ms=100))

# New API (direct)
sim = get_simulator()
sim.configure(NetworkConfig(latency_ms=100))
```

---

## Performance Expectations

| Operation | Current (SQLite) | New (ns.py) |
|-----------|------------------|-------------|
| send() | ~0.1-1ms (INSERT) | ~0.001ms (in-memory) |
| drain() | ~0.1-1ms (SELECT+DELETE) | ~0.01ms (SimPy run) |
| Memory per packet | SQLite row + Python bytes | Python object |
| 1000 packets/tick | 100-1000ms | 1-10ms |

---

## Testing Plan

1. **Unit tests** - `tests/test_nspy_network.py`
   - Latency delivery timing
   - Jitter distribution
   - Packet loss rates
   - Token bucket behavior
   - Gilbert-Elliott loss patterns
   - NAT integration

2. **Integration tests** - Update existing `test_network_simulator.py`
   - All existing tests should pass

3. **Scenario tests** - Update `test_network_scenarios.py`
   - Real-world network profiles (mobile, satellite, etc.)

4. **Performance benchmarks**
   - Throughput: packets/second
   - Memory: bytes per queued packet
   - Latency: time to process batch

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| SimPy complexity | Start with simple pipeline, add features incrementally |
| ns.py API changes | Pin version, wrap in adapter |
| NAT integration issues | Keep existing NAT code, just change packet delivery |
| Performance regression | Benchmark before/after, optimize if needed |
| Test flakiness | Use deterministic RNG seeds |

---

## Files Changed

### New Files
- `simulator/nspy_network.py` - Main simulator
- `simulator/loss_models.py` - Loss model implementations
- `tests/test_nspy_network.py` - Unit tests

### Modified Files
- `queues.py` - Add adapter layer
- `network_config.py` - Keep NAT, remove packet queue globals
- `simulator/network.py` - Use new simulator internally

### Deleted (Phase 3)
- Bandwidth tracking globals in `network_config.py`
- Burst loss globals in `network_config.py`
- SQLite packet queue code

---

## Timeline

- **Phase 1**: Core simulator + tests (this work)
- **Phase 2**: Integration with existing code
- **Phase 3**: Remove old implementation, full migration

---

## Open Questions

1. **Per-link vs global bandwidth?** Current is global; should we support per-peer-pair limits?
2. **Packet reordering?** ns.py Wire prevents reordering; is this desired?
3. **Queue size limits?** Current has none; should we add buffer overflow simulation?
