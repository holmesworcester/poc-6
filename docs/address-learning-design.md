# Address Learning Design

> **Related:** See `docs/planning/connection-abstraction-design.md` (from proj-v2-recorded branch) for the full connection layer design. Address learning is part of the Connection layer.

## Problem

Two CLI peers need to establish UDP communication. The challenge: only the joiner (Bob) knows the inviter's (Alice) address initially (from the invite URL). Alice needs to learn Bob's address from incoming packets.

Currently, address routing logic lives in test infrastructure (`tests/networking/conftest.py`) instead of production code. This is backwards - tests should exercise real code, not implement features.

## Architecture: Where Address Learning Fits

From the connection-abstraction-design.md, the system has two layers:

| Layer | Responsibility |
|-------|---------------|
| **Connection** | Transit key management, routing, **address management** |
| **Sync** | Bloom filters, event exchange |

Address learning belongs in the **Connection layer**:
1. `Connection.receive()` handles incoming blobs WITH source address
2. Connection learns the address when blob arrives
3. `Connection.send()` uses learned addresses for routing
4. Sync just calls `connection.send(blob)` - doesn't know about addresses

## Bootstrap Flow

```
1. Alice creates network and invite
   - Invite URL contains Alice's ip:port

2. Bob accepts invite
   - invite_accepteds stores Alice's ip:port
   - Bob creates Connection to Alice using invite address

3. Bob sends connection request to Alice
   - UDP packet has Bob's address as SOURCE
   - Alice receives packet

4. Alice learns Bob's address (IN CONNECTION LAYER)
   - Connection.receive() gets (blob, source_addr)
   - Extracts sender's connection_id from packet
   - Updates connection with learned address
   - NOW Alice can respond to Bob

5. Bidirectional communication established
```

## Schema Changes

Add address columns to the `connections` table (from connection-abstraction-design.md):

```sql
CREATE TABLE connections (
    connection_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,

    -- Identity labels
    peer_shared_id TEXT,
    invite_id TEXT,

    -- Keys
    our_key BLOB NOT NULL,
    their_connection_id TEXT,
    their_key BLOB,

    -- Network address (NEW - for address learning)
    peer_ip TEXT,                   -- Learned from UDP source
    peer_port INTEGER,              -- Learned from UDP source
    address_source TEXT,            -- 'packet', 'invite', 'manual'
    address_learned_ms INTEGER,     -- When we learned it

    -- Lifecycle
    created_at INTEGER NOT NULL,
    last_handshake_ms INTEGER NOT NULL,
    ttl_ms INTEGER NOT NULL DEFAULT 300000,

    PRIMARY KEY (connection_id, recorded_by)
);
```

## Implementation

### Step 1: Add address columns to connections table

**File:** `events/network/connection.sql`

Add the four address columns shown above.

### Step 2: Add learn_address() to connection.py

**File:** `events/network/connection.py`

```python
def learn_address(connection_id: str, peer_ip: str, peer_port: int,
                  source: str, t_ms: int, recorded_by: str, db):
    """Update connection with learned address from incoming packet."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute("""
        UPDATE connections SET
            peer_ip = ?, peer_port = ?,
            address_source = ?, address_learned_ms = ?
        WHERE connection_id = ? AND recorded_by = ?
    """, (peer_ip, peer_port, source, t_ms, connection_id, recorded_by))
```

### Step 3: Add get_address() to connection.py

**File:** `events/network/connection.py`

```python
def get_address(peer_shared_id: str, recorded_by: str, db) -> tuple | None:
    """Get address for a peer's connection."""
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Priority 1: Learned address on connection
    conn = safedb.query_one("""
        SELECT peer_ip, peer_port FROM connections
        WHERE peer_shared_id = ? AND recorded_by = ? AND peer_ip IS NOT NULL
        ORDER BY address_learned_ms DESC LIMIT 1
    """, (peer_shared_id, recorded_by))
    if conn and conn['peer_ip']:
        return (conn['peer_ip'], conn['peer_port'])

    # Priority 2: Bootstrap address from invite_accepteds
    invite = safedb.query_one("""
        SELECT address, port FROM invite_accepteds
        WHERE inviter_peer_shared_id = ? AND recorded_by = ?
    """, (peer_shared_id, recorded_by))
    if invite and invite['address']:
        return (invite['address'], invite['port'])

    return None
```

### Step 4: Pass source address through incoming queue

**File:** `core/queues.sql`

```sql
-- Add columns to incoming_queue
ALTER TABLE incoming_queue ADD COLUMN source_ip TEXT;
ALTER TABLE incoming_queue ADD COLUMN source_port INTEGER;
```

**File:** `core/queues.py`

Update `add_immediate()` to accept and store source address.

### Step 5: Call learn_address() when receiving

**File:** `events/network/connection.py` (in receive path)

When processing incoming blobs, call `learn_address()` with the UDP source address.

### Step 6: Simplify test infrastructure

**File:** `tests/networking/conftest.py`

- Remove `Client.peer_addresses` dict
- Transport callback calls `connection.get_address()`
- `add_peer_address()` creates database entry via `learn_address()`

## Files to Modify

| File | Changes |
|------|---------|
| `events/network/connection.sql` | Add peer_ip, peer_port, address_source, address_learned_ms |
| `events/network/connection.py` | Add learn_address(), get_address() |
| `core/queues.sql` | Add source_ip, source_port |
| `core/queues.py` | Pass source_addr through |
| `tests/networking/conftest.py` | Use Connection layer for routing |

## Verification

```bash
PYTHONPATH=. pytest tests/networking/test_bootstrap_with_invite.py -v
```

All tests should pass with real production code, not test infrastructure hacks.
