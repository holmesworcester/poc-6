# Real Networking Tests Rules

Guidelines for writing tests in `tests/networking/`.

## Running These Tests

Networking tests CAN run in parallel because the transport callback is thread-local.
Each test thread gets its own isolated callback.

```bash
# Run networking tests (parallel OK)
PYTHONPATH=. pytest tests/networking/ -v

# Run all tests
PYTHONPATH=. pytest tests/ -v
```

## What These Tests Are

Real networking tests verify that **separate clients with separate databases** can communicate over **actual UDP sockets** on localhost. This is the closest we can get to production networking without multiple processes.

## How They Differ from Scenario Tests

| Aspect | Scenario Tests | Networking Tests |
|--------|----------------|------------------|
| Database | Single shared SQLite | Separate SQLite per client |
| Network | Simulated (in-memory queue) | Real UDP sockets on localhost |
| Time | Simulated (controlled `t_ms`) | Wall-clock time |
| Isolation | Peers share blobs table | Each client has independent state |

## Key Concepts

### RealClient

Each test client wraps:
- **Own SQLite database** - completely isolated state
- **UDP socket** - bound to a unique localhost port
- **peer_id, peer_shared_id, user_id** - identity after joining

```python
alice = RealClient("Alice", f"{tmp_dir}/alice.db", port=19001)
bob = RealClient("Bob", f"{tmp_dir}/bob.db", port=19002)
```

### Transport Callback

Routes packets via UDP instead of the simulator:

```python
def route_packet(blob: bytes, from_peer: str, to_peer: str, t_ms: int) -> bool:
    # Find destination client by peer_shared_id
    # Send via UDP socket
    # Return True to indicate packet was handled (don't use simulator)
```

**Important**: Return `True` for ALL packets to prevent simulator fallback. Unknown destinations should be dropped, not queued.

### Tick Loop

Each client's tick:
1. Drains UDP packets into its incoming queue
2. Runs `tick_module.tick()` to process events
3. Commits the transaction

```python
def tick(self, t_ms: int):
    self.receive_udp_packets(t_ms)
    tick_module.tick(t_ms=t_ms, db=self.db)
    self.db.commit()
```

### Wall-Clock Time

Always use wall-clock time:

```python
def now_ms():
    return int(time.time() * 1000)
```

Never use simulated time offsets in networking tests.

## Rules

### 1. Use the fixtures

Use `conftest.py` fixtures for client creation and cleanup:

```python
def test_something(real_network):
    alice, bob = real_network["alice"], real_network["bob"]
```

### 2. Always clean up transport callback

The transport callback is global. Always reset it:

```python
finally:
    queues.set_transport_callback(None)
```

The fixture handles this automatically.

### 3. Use unique ports

Each test run should use unique ports to avoid conflicts:

```python
# Good: Base port + offset
alice_port = 19001 + (test_id * 10)

# Bad: Hardcoded ports that may conflict
alice_port = 19001
```

The fixture handles port allocation automatically.

### 4. Allow enough ticks for sync

Real networking has actual latency. Allow sufficient ticks:

```python
for i in range(100):  # Up to 100 ticks
    t_ms = now_ms()
    alice.tick(t_ms)
    bob.tick(t_ms)
    time.sleep(0.05)  # 50ms between ticks

    if check_sync_complete():
        break
```

### 5. Don't test simulator behavior

These tests verify real networking, not the simulator. Don't test:
- Packet loss simulation
- Latency simulation
- Bandwidth limits

Those belong in `tests/scenario_tests/`.

### 6. Verify both directions

Always verify sync works in both directions:

```python
# Alice sends to Bob
alice_msg = message.create(..., db=alice.db)
# ... tick until synced ...
assert message.list(..., db=bob.db)  # Bob has it

# Bob sends to Alice
bob_msg = message.create(..., db=bob.db)
# ... tick until synced ...
assert message.list(..., db=alice.db)  # Alice has it
```

### 7. Use assertions with context

When assertions fail, include debugging context:

```python
bob_messages = message.list(channel_id, bob.peer_id, bob.db)
assert len(bob_messages) == 1, f"Expected 1 message, got {len(bob_messages)}. Ticks: {tick_count}"
```

## Common Patterns

### Creating a network with multiple clients

```python
# 1. Create clients with separate databases
alice = RealClient("Alice", f"{tmp_dir}/alice.db", port=19001)
bob = RealClient("Bob", f"{tmp_dir}/bob.db", port=19002)
charlie = RealClient("Charlie", f"{tmp_dir}/charlie.db", port=19003)

# 2. Set up transport callback
setup_transport_callback({"alice": alice, "bob": bob, "charlie": charlie})

# 3. Alice creates network
result = user.new_network(name="TestNet", t_ms=now_ms(), db=alice.db)
alice.peer_id = result['peer_id']
# ... etc

# 4. Create and distribute invite
invite_id, invite_code, _ = invite.create(peer_id=alice.peer_id, ...)

# 5. Others join
bob_result = user.join(peer_id=bob.peer_id, invite_link=invite_code, ...)
charlie_result = user.join(peer_id=charlie.peer_id, invite_link=invite_code, ...)
```

### Waiting for sync with timeout

```python
def wait_for_sync(check_fn, max_ticks=100, tick_interval=0.05):
    """Wait for condition with tick loop."""
    for i in range(max_ticks):
        t_ms = now_ms()
        for client in clients.values():
            client.tick(t_ms)
        time.sleep(tick_interval)

        if check_fn():
            return i + 1

    return None  # Timeout

ticks = wait_for_sync(lambda: len(message.list(..., db=bob.db)) > 0)
assert ticks is not None, "Sync timed out"
```

## Debugging

### Enable logging

```python
import logging
logging.basicConfig(level=logging.INFO, format='%(name)s: %(message)s')

# Suppress noisy modules
for name in ['events', 'crypto', 'store', 'tick', 'sync', 'queues', 'db']:
    logging.getLogger(name).setLevel(logging.WARNING)
```

### Check packet routing

Add logging to the transport callback:

```python
def route_packet(blob, from_peer, to_peer, t_ms):
    log.info(f"ROUTE: {from_peer[:10]}... -> {to_peer[:10]}... ({len(blob)}B)")
    # ...
```

### Inspect database state

```python
# Check what Bob has
bob_events = bob.db._conn.execute(
    "SELECT event_id, type FROM valid_events WHERE recorded_by = ?",
    (bob.peer_id,)
).fetchall()
print(f"Bob has {len(bob_events)} valid events")
```
