# Real Networking Tests Rules

These tests verify actual UDP packet flow between separate databases. They must follow strict rules to ensure we're testing real networking, not simulation artifacts.

## Core Principles

### 1. NO LOOPBACK FALLBACK
The transport layer has a loopback fallback for when addresses aren't known. **Real networking tests MUST NOT rely on this.** If a test passes because of loopback, it's not testing real networking.

- Always register peer addresses explicitly via `transport.add_peer_address()`
- Verify packets actually traverse UDP sockets
- Fail fast if address lookup returns None

### 2. OUT-OF-BAND ADDRESS EXCHANGE
In real life, the invite link alone doesn't establish a connection. The inviter's address must be communicated separately (QR code includes it, manual entry, etc.).

**Future**: IP/port should be embedded in invite links. Until then, tests must simulate out-of-band address exchange.

### 3. SEPARATE DATABASES
Each client has its own SQLite database. They communicate ONLY via UDP packets. No shared state, no peer-scoped views of a single DB.

### 4. API-ONLY ASSERTIONS
**Do NOT write custom SQL queries in test assertions.** Use the module APIs:

```python
# GOOD - use API functions
from events.network import connection
conns = connection.get_connections(peer_id, t_ms, db)
assert len([c for c in conns if c.can_send()]) >= 1

# BAD - custom SQL
result = db.query("SELECT * FROM connections WHERE ...")
```

This ensures tests verify actual behavior, not implementation details.

### 5. NO CHEATING WITH HINTS
Connection hints (the 16-byte key IDs) route packets to the right peer. Tests must not pre-populate routing tables or bypass hint-based routing.

## Test Structure

```python
def test_real_networking_example(create_client):
    # 1. Create clients with separate DBs
    alice = create_client("alice")
    bob = create_client("bob")

    # 2. Alice creates network/invite
    alice_result = user.new_network(...)
    invite_id, invite_link, _ = invite.create(...)

    # 3. Out-of-band: Bob learns Alice's address
    transport.add_peer_address(alice.peer_shared_id, '127.0.0.1', alice.network.port)

    # 4. Bob joins using invite link
    bob_result = user.join(peer_id=..., invite_link=invite_link, ...)

    # 5. Tick both clients, routing UDP packets
    for i in range(N):
        route_udp_packets([alice, bob])
        alice.tick(t_ms)
        bob.tick(t_ms)
        t_ms += 50  # 50ms tick interval matches most granular job

    # 6. Assert using API calls
    alice_conns = connection.get_connections(alice.peer_id, t_ms, alice.db)
    assert len([c for c in alice_conns if c.can_send()]) >= 1
```

## Debugging

If packets aren't flowing:
1. Check `transport.pending_count()` - are packets being queued?
2. Check `client.network.drain()` - are UDP packets arriving?
3. Enable logging: `logging.getLogger('events.network.connection').setLevel(logging.DEBUG)`
