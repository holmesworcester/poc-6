# Proposed Protocol Spec Changes: Connection Abstraction

This document describes proposed changes to the Connection and Sync sections of `quiet-protocol-specification.md`.

---

## Change 1: Connection Table Schema

**Location**: Section "Connection Table" (around line 601)

**Current**:
```
connections (
    peer_shared_id,         -- remote peer's public identity
    our_transit_key_id,     -- key ID we sent (for matching acks, routing via owner_peer_id)
    their_transit_key_id,   -- key ID they provided (for nonce derivation)
    their_transit_key,      -- symmetric key to send TO them
    origin_ip, origin_port,
    last_seen_ms, ttl_ms
)
```

**Proposed**:
```
connections (
    our_transit_key_id PRIMARY KEY,  -- key we gave them (routes incoming blobs)

    -- Identity labels (at least one required)
    peer_shared_id,         -- remote peer's public identity (NULL until synced)
    invite_id,              -- invite used for this connection (for bootstrap)

    -- Their key (we send to them)
    their_transit_key_id,   -- key ID they provided (for nonce derivation)
    their_transit_key,      -- symmetric key to send TO them

    -- Network
    origin_ip, origin_port,

    -- Lifecycle
    last_seen_ms, ttl_ms,

    CHECK (peer_shared_id IS NOT NULL OR invite_id IS NOT NULL)
)
```

**Rationale**:
- `our_transit_key_id` becomes primary key for routing (incoming blobs use this as hint)
- `invite_id` enables labeling connections during bootstrap before `peer_shared` syncs
- CHECK constraint ensures every connection has at least one identity label

---

## Change 2: New Section "Connection Identity and Bootstrap"

**Location**: After "Connection Table", before "Lifecycle"

**Add new section**:

```markdown
## Connection Identity and Bootstrap

Connections are keyed by `our_transit_key_id` — the transit key we gave the remote peer. This key appears as the hint in the first 32 bytes of incoming blobs, enabling routing before decryption.

### Identity Labels

Each connection has one or both identity labels:

- **`peer_shared_id`**: The remote peer's public identity. Set after their `peer_shared` event syncs and is validated.
- **`invite_id`**: The invite used to establish this connection. Set when `sync_connect` includes an `invite_id` field (indicating the sender is a new joiner).

During bootstrap, before `peer_shared` has synced:
1. Joiner sends `sync_connect` with `signed_by: invite_id` and includes `invite_id` field
2. Inviter validates the invite signature and stores `invite_id` as the connection label
3. Connection is usable for sync immediately
4. When joiner's `peer_shared` arrives and validates, connection upgrades to include `peer_shared_id`

This allows bidirectional sync to begin before the DAG knowledge catches up.

### Label Upgrade

When a `peer_shared` event projects that corresponds to an existing connection (matching by invite lineage or direct observation):

```
UPDATE connections
SET peer_shared_id = :peer_shared_id
WHERE invite_id = :invite_id AND peer_shared_id IS NULL
```

The `invite_id` is retained for audit and debugging purposes.
```

---

## Change 3: New Section "Connection Inbox and Routing"

**Location**: After "Connection Identity and Bootstrap"

**Add new section**:

```markdown
## Connection Inbox and Routing

Each connection has an associated inbox for early routing of incoming blobs.

### Inbox Table

```
connection_inbox (
    id PRIMARY KEY,
    our_transit_key_id,     -- routes to connection
    blob,                   -- raw transit-wrapped blob
    received_at,
    FOREIGN KEY (our_transit_key_id) REFERENCES connections(our_transit_key_id)
)
```

### Routing Flow

When a blob arrives from the network:

1. Extract hint from first 32 bytes of blob
2. Look up connection by `our_transit_key_id = hint`
3. If found: insert into `connection_inbox` for that connection
4. If not found: blob is either a new handshake or stale; route to fallback queue

This "early routing" means:
- Blobs are associated with connections before unwrapping
- Sync processes blobs per-connection, not from a global queue
- Unknown keys don't pollute connection inboxes

### Receiving from a Connection

To receive from a connection:

1. Query `connection_inbox WHERE our_transit_key_id = :key ORDER BY received_at`
2. For each blob: unwrap using `our_transit_key`, discover `local_peer_id` from key ownership
3. Return `[(unwrapped_blob, local_peer_id), ...]`
4. Delete processed rows from inbox

The `local_peer_id` comes from `transit_keys.owner_peer_id` — the peer who created the transit key.
```

---

## Change 4: Update "Multi-Account Routing"

**Location**: Section "Multi-Account Routing" (around line 635)

**Current** (partial):
```markdown
## Multi-Account Routing

On devices with multiple local peers (linked accounts), incoming messages must be routed to the correct local peer. This is handled by the transit key:

1. Each `transit_key` and `transit_prekey` has an `owner_peer_id` (the local peer that created it)
2. When receiving a wrapped message, try decryption with available transit keys
3. The key that successfully decrypts identifies the target local peer via `owner_peer_id`
4. Process the message under that peer's context (`recorded_by = owner_peer_id`)
```

**Proposed**:
```markdown
## Multi-Account Routing

On devices with multiple local peers (linked accounts), incoming messages must be routed to the correct local peer. Routing happens in two stages:

### Stage 1: Route to Connection (by hint)

The first 32 bytes of every transit-wrapped blob is a hint matching `our_transit_key_id`. This routes the blob to the correct connection's inbox without decryption.

### Stage 2: Route to Local Peer (by key ownership)

When processing a connection's inbox:

1. Unwrap blob using `our_transit_key` (the key we gave them)
2. Look up `transit_keys.owner_peer_id` for that key
3. The `owner_peer_id` identifies which local peer should process this blob
4. Process under that peer's context (`recorded_by = owner_peer_id`)

This two-stage routing means:
- Blobs reach the right connection immediately (no decryption needed)
- Local peer assignment happens during unwrap (natural point in the flow)
- Connections are device-wide but processing is peer-specific
```

---

## Change 5: Update Sync Section

**Location**: Section "Sync" (around line 646)

**Current** (partial):
```markdown
# Sync

To sync data, peers periodically send `sync` events to all connections.
```

**Proposed** (add after first paragraph):
```markdown
# Sync

To sync data, peers periodically send `sync` events to all connections.

### Connection Interface

Sync operates through a connection abstraction rather than directly on queues:

```
# Sending
for connection in get_all_connections():
    sync_request = build_sync_request(local_peer, connection.label)
    connection.send(sync_request)

# Receiving
for connection in get_all_connections():
    for (blob, local_peer_id) in connection.receive():
        process_sync_event(blob, local_peer_id, connection)
```

This abstraction provides:
- **Separation of concerns**: Sync logic doesn't manage transit keys or routing
- **Per-connection semantics**: Each connection has its own receive queue
- **Testability**: Connections can be mocked for sync testing
- **Identity context**: `connection.label` provides peer identity or invite_id for sync state tracking
```

---

## Summary of Changes

| Section | Change Type | Description |
|---------|-------------|-------------|
| Connection Table | Modify | Primary key on `our_transit_key_id`, add `invite_id` |
| Connection Identity and Bootstrap | Add | New section on identity labels and upgrade flow |
| Connection Inbox and Routing | Add | New section on per-connection inbox and early routing |
| Multi-Account Routing | Modify | Two-stage routing explanation |
| Sync | Modify | Add connection interface abstraction |

## Backward Compatibility

These changes are additive:
- Existing connections continue to work (peer_shared_id remains valid)
- New invite_id field is optional (NULL for established peers)
- Connection inbox is an implementation detail, not a wire format change
- Sync events unchanged on the wire
