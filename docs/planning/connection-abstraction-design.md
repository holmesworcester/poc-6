# Connection Abstraction Design

## Overview

This document describes a refactoring to abstract connections from the syncing algorithm, creating a cleaner separation between the connection layer (transit key management, routing) and the sync layer (bloom filters, event exchange).

## Goals

1. **Clean separation**: Sync operates on Connection objects with `send(event)` / `receive()` methods
2. **Early routing**: Route blobs to connections by transit key hint before sync processes them
3. **Bootstrap identity**: Use `invite_id` to label connections before `peer_shared` syncs

## Key Insight: Connections Exist at Transit Layer

Connections exist **before we know which local peer they're for**. The transit key hint routes to the connection; only after unwrap do we discover the local peer (via `transit_keys.owner_peer_id`).

```
blob arrives → hint (first 32 bytes) → matches connection.our_transit_key_id
                                     → routes to connection's inbox
                                     → unwrap reveals local_peer_id
```

This is different from a "per-peer connection" model. Connections are keyed by transit keys, not by peer identity. <!-- but peer identity -->

## Current Architecture

### Components
1. **sync_connect.py** - Two-way handshake (sync_connect → sync_connect_ack)
2. **sync.py** - Bloom-based window protocol over established connections
3. **queues.incoming** - Device-wide blob queue, routing at unwrap time
4. **sync_connections table** - Connection state (peer_shared_id, transit keys, ttl)

### Current Flow
```
sync_connect.send_connect_to_all()
  → send_connect() for each known peer
  → sync_connect event queued

sync_connect.project()
  → validates, stores connection in sync_connections
  → sends sync_connect_ack back

sync.send_request_to_all()
  → queries sync_connections table directly
  → sends sync requests to each connection

sync.receive()
  → drains incoming queue
  → routes via transit key hint (late!)
  → unwraps and projects
```

### Problems
1. **Late routing**: Transit key lookup happens after dequeue, deep in `unwrap_and_store`
2. **No bootstrap identity**: Before `peer_shared` syncs, we can't label who we're talking to
3. **Tight coupling**: sync.py queries sync_connections directly
4. **No per-connection semantics**: Single device-wide queue

## Proposed Design

### Connection Class

```python
@dataclass
class Connection:
    """Bidirectional channel keyed by transit keys, labeled by identity."""

    # Primary key: the transit key we gave them (they send to us)
    our_transit_key_id: str

    # Identity labels (at least one set, can upgrade over time)
    peer_shared_id: str | None  # Set after peer_shared syncs
    invite_id: str | None       # Set for bootstrap connections

    # Their key (we send to them)
    their_transit_key: bytes
    their_transit_key_id: str   # For nonce derivation

    # State
    last_seen_ms: int
    ttl_ms: int

    def send(self, event_blob: bytes, t_ms: int, db) -> None:
        """Wrap event with their_transit_key and add to outgoing."""

    def process_inbox(self, t_ms: int, db) -> None:
        """Process pending blobs: unwrap, create recorded events, trigger projection.
        Called by tick() or network receive handler."""

    def is_active(self, t_ms: int) -> bool:
        """Check if connection is still valid (not expired)."""

    @property
    def label(self) -> str:
        """Human-readable identity: peer_shared_id or invite_id."""
        return self.peer_shared_id or self.invite_id
```

### ConnectionManager

```python
class ConnectionManager:
    """Manages all connections for a device (transit-layer routing)."""

    def get_all_connections(self, t_ms: int, db) -> list[Connection]:
        """Get all active connections on this device."""

    def get_connection_by_key(self, our_transit_key_id: str, db) -> Connection | None:
        """Look up connection by the key we gave them (for routing)."""

    def get_connections_for_peer(self, peer_shared_id: str, t_ms: int, db) -> list[Connection]:
        """Get connections labeled with this peer identity."""

    def route_incoming(self, blob: bytes, t_ms: int, db) -> bool:
        """Route blob to connection inbox by transit key hint.
        Returns False if key unknown (use fallback queue for handshakes)."""
```

### Simplified Sync Interface

```python
# sync.py becomes send-only

def send_requests(conn_manager: ConnectionManager, from_peer_id: str, t_ms: int, db):
    """Send sync requests to all connections."""
    for conn in conn_manager.get_all_connections(t_ms, db):
        request = build_sync_request(from_peer_id, conn.label, t_ms, db)
        conn.send(request, t_ms, db)

# No sync receive needed - connections handle that directly
```

Sync doesn't need a receive path because connections handle the full receive lifecycle:
1. Connection receives blob from inbox
2. Connection unwraps and determines `local_peer_id` from key ownership
3. Connection creates `recorded` event for that peer
4. Normal projection handles everything from there

This means sync is purely about *requesting* events via bloom filters. Responses flow through the standard recorded → projection path like any other incoming event.

### invite_id as Connection Label

Before `peer_shared` syncs, label connections by `invite_id`:

1. **Joiner side**: Creates connection labeled with `invite_id` they used
2. **Inviter side**: Receives sync_connect signed by invite, labels connection with `invite_id`
3. **After sync**: Once `peer_shared` arrives, upgrade label to `peer_shared_id` <!-- we could just make a new connection here and drop the old one -->

```python
# In sync_connect.project() when invite_id is present:
connection.invite_id = event_data.get('invite_id')
# peer_shared_id stays NULL until peer_shared event syncs

# Later, in peer_shared.project():
connection.upgrade_identity(peer_shared_id)
```

## Schema Changes

### connection_attempts table (SUBJECTIVE - pending handshakes)

Tracks outgoing `sync_connect` messages waiting for acknowledgement. This is **not** a connection yet — we don't have their transit key, so we can't send to them.

```sql
-- Add to SUBJECTIVE_TABLES in db.py
CREATE TABLE connection_attempts (
    our_transit_key_id TEXT NOT NULL,   -- Key we sent them
    recorded_by TEXT NOT NULL,          -- Local peer who initiated

    -- Target identity
    to_peer_shared_id TEXT,             -- Who we're trying to connect to
    invite_id TEXT,                     -- Invite used (for bootstrap)

    -- Lifecycle
    created_at INTEGER NOT NULL,
    ttl_ms INTEGER NOT NULL DEFAULT 300000,

    PRIMARY KEY (our_transit_key_id, recorded_by),
    CHECK (to_peer_shared_id IS NOT NULL OR invite_id IS NOT NULL)
);

CREATE INDEX idx_connection_attempts_recorded_by ON connection_attempts(recorded_by);
```

**Key insight**: A "connection" requires both directions — we can send to them AND they can send to us. Before receiving their transit key, we only have half the channel. The `connection_attempts` table tracks these pending half-channels.

### Handshake Flow with connection_attempts

```
send_connect(to_peer):
  1. Create transit_key (our key for them to send to us)
  2. Insert into connection_attempts (our_transit_key_id, to_peer_shared_id)
  3. Wrap sync_connect with their prekey/connection, queue for delivery
  4. NO connection entry yet - we can't send to them

receive sync_connect (their connect to us):
  1. Extract their transit_key from event
  2. Create connection entry WITH their_transit_key (we can now send to them)
  3. Send sync_connect_ack back with OUR transit_key

receive sync_connect_ack (response to our connect):
  1. Look up connection_attempt by for_transit_key_id (the key we sent)
  2. Extract their transit_key from ack
  3. Create connection entry WITH their_transit_key
  4. Delete the connection_attempt (handshake complete)
```

This ensures:
- `connections` only contains usable bidirectional channels
- `connection_attempts` is clearly ephemeral/pending state
- We never have a "connection" we can't actually use

### connections table (SUBJECTIVE - established bidirectional channels)

Only contains entries where we have `their_transit_key` — meaning we can actually send to them.

```sql
-- Add to SUBJECTIVE_TABLES in db.py (enables SafeDB access)
CREATE TABLE connections (
    our_transit_key_id TEXT NOT NULL,
    recorded_by TEXT NOT NULL,      -- local peer who owns this connection

    -- Identity labels (at least one required)
    peer_shared_id TEXT,            -- NULL until peer_shared syncs
    invite_id TEXT,                 -- For bootstrap connections

    -- Their key (we send to them) - REQUIRED for a real connection
    their_transit_key_id TEXT NOT NULL,
    their_transit_key BLOB NOT NULL,

    -- State
    last_seen_ms INTEGER,
    ttl_ms INTEGER,

    PRIMARY KEY (our_transit_key_id, recorded_by),
    CHECK (peer_shared_id IS NOT NULL OR invite_id IS NOT NULL)
);

CREATE INDEX idx_connections_recorded_by ON connections(recorded_by);

-- Partial indexes: only index when we have the identity
CREATE INDEX idx_connections_peer ON connections(peer_shared_id, recorded_by)
    WHERE peer_shared_id IS NOT NULL;
CREATE INDEX idx_connections_invite ON connections(invite_id, recorded_by)
    WHERE invite_id IS NOT NULL;
```

The `recorded_by` is denormalized from `transit_keys.owner_peer_id` — we store it directly so SafeDB can enforce scoping.

Lookup patterns:
```python
# By remote peer identity (when we have it)
conn = safedb.query_one(
    "SELECT * FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
    (remote_peer_id, local_peer_id)
)

# During bootstrap, by invite_id
conn = safedb.query_one(
    "SELECT * FROM connections WHERE invite_id = ? AND recorded_by = ?",
    (invite_id, local_peer_id)
)

# Get all my connections (either identity works)
my_connections = safedb.query(
    "SELECT * FROM connections WHERE recorded_by = ? AND last_seen_ms + ttl_ms > ?",
    (local_peer_id, t_ms)
)
```

### connection_inbox table (DEVICE-WIDE - for routing)

```sql
-- Add to DEVICE_TABLES in db.py (UnsafeDB access for routing)
CREATE TABLE connection_inbox (
    id INTEGER PRIMARY KEY,
    our_transit_key_id TEXT NOT NULL,  -- hint from blob, no FK to subjective table
    blob BLOB NOT NULL,
    received_at INTEGER NOT NULL
);

CREATE INDEX idx_inbox_key ON connection_inbox(our_transit_key_id);
```

No foreign key to connections because inbox is device-wide and connections is peer-scoped.

## Connection Interface

The connection layer provides three methods with clear scoping:

```python
# Send to a specific connection
connection.send(connection_id, blob)

# Receive for a specific peer (SafeDB-scoped because peer_id is passed)
connection.receive(peer_id)

# Process the device-wide inbox, routing to receive()
connection.process_inbox()
```

### process_inbox() — Device-wide (UnsafeDB)

Routes blobs from inbox to peer-scoped `receive()`:

```python
def process_inbox(t_ms: int, db: Database) -> None:
    """Drain inbox, route by transit_key_id to receive(peer_id)."""
    unsafedb = create_unsafe_db(db)

    entries = unsafedb.query(
        "SELECT id, our_transit_key_id, blob FROM connection_inbox ORDER BY received_at"
    )

    for entry in entries:
        # Look up transit key to find owner peer
        key_row = unsafedb.query_one(
            "SELECT owner_peer_id FROM transit_keys WHERE key_id = ?",
            (entry['our_transit_key_id'],)
        )

        if key_row:
            # Route to peer-scoped receive
            receive(key_row['owner_peer_id'], entry['our_transit_key_id'], entry['blob'], t_ms, db)

        # Delete processed (or orphaned) entry
        unsafedb.execute("DELETE FROM connection_inbox WHERE id = ?", (entry['id'],))
```

### receive(peer_id, ...) — Peer-scoped (SafeDB)

```python
def receive(peer_id: str, transit_key_id: str, blob: bytes, t_ms: int, db: Database) -> None:
    """Process blob for this peer. SafeDB-scoped because peer_id is passed."""
    safedb = create_safe_db(db, recorded_by=peer_id)

    conn = safedb.query_one(
        "SELECT * FROM connections WHERE our_transit_key_id = ? AND recorded_by = ?",
        (transit_key_id, peer_id)
    )

    if conn:
        # Unwrap, create recorded event, trigger projection
        unwrapped = unwrap_blob(blob, conn['their_transit_key'])
        create_recorded_event(unwrapped, peer_id, t_ms, db)
        # Normal projection handles the rest
```

### Sync uses send() only

```python
# Sync gets only this peer's connections (SafeDB)
safedb = create_safe_db(db, recorded_by=peer_id)
my_connections = safedb.query(
    "SELECT * FROM connections WHERE recorded_by = ? AND last_seen_ms + ttl_ms > ?",
    (peer_id, t_ms)
)

for conn in my_connections:
    request = build_sync_request(peer_id, conn['peer_shared_id'] or conn['invite_id'])
    connection.send(conn['our_transit_key_id'], request)
```

Scoping is clean:
- `process_inbox()` is device-wide (UnsafeDB) — just routing by transit key
- `receive(peer_id)` is peer-scoped (SafeDB) — all the real work
- Sync only uses `send()` and queries connections via SafeDB

## Migration Path

### Phase 1: Connection Class (Wrap Existing)
Create `connection.py` wrapping existing `sync_connections` table. No schema changes yet.

### Phase 2: Add invite_id
Add `invite_id` column to sync_connections. Update `sync_connect.project()` to store it.

### Phase 3: Per-Connection Inbox
Add `connection_inbox` table. Implement early routing. Keep fallback queue for handshakes.

### Phase 4: Refactor sync.py
Replace direct queue/table access with ConnectionManager methods.

## Files to Modify

| File | Phase | Changes |
|------|-------|---------|
| **New**: `connection.py` | 1 | Connection and ConnectionManager classes |
| `events/network/sync_connect.py` | 2 | Store invite_id in connection |
| `events/network/sync_connect_ack.py` | 2 | Use ConnectionManager for lookup |
| `queues.py` | 3 | Add route_incoming(), keep incoming for handshakes |
| Schema/migrations | 3 | Rename table, add invite_id, add connection_inbox |
| `events/network/sync.py` | 4 | Use Connection.send/receive |
| `events/identity/peer_shared.py` | 4 | Upgrade connection label when projecting |

## Benefits

1. **Testability**: Sync can be tested with mock connections
2. **Clarity**: Connection establishment vs data sync are separate concerns
3. **Bootstrap support**: invite_id labeling works before peer_shared syncs
4. **Performance**: Early routing avoids processing blobs for unknown keys
5. **Extensibility**: Connection class can add metrics, rate limiting, etc.
