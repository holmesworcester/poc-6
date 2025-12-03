# Connection Handshake (Two-Way)

## Goal

Implement a two-way connection handshake that decouples connection establishment from DAG knowledge.

## Why

Without two-way handshake:
1. Bob sends `sync_connect` to Alice (can sign with invite)
2. Alice sends `sync_connect` back to Bob
3. **Problem**: Bob can't verify Alice's signature - he doesn't have Alice's `peer_shared` yet (requires sync, which requires connection)

With two-way handshake:
1. Bob sends `sync_connect` with `bob_transit_key`
2. Alice sends `sync_connect_ack` with `alice_transit_key`, **wrapped to bob's key**
3. Bob authenticates the ack implicitly: if he can decrypt it, it came from whoever has Alice's transit_prekey private key

## Spec Reference

See `docs/quiet-protocol-specification.md`:
- Section: "Connection" (entirely rewritten)
- Subsections: "Why Two-Way Handshake", "Handshake Protocol", "Verification"
- Types table: `sync_connect` and `sync_connect_ack`

## Current State

**sync_connect.py** currently:
- Sends one-way connection with `sig_invite` and `sig_peer` fields
- No ack mechanism
- Verification requires either invite or peer_shared to be known

## Tasks

### 1. Simplify `sync_connect` event structure
Current fields (remove conditional ones):
```python
{
    'type': 'sync_connect',
    'peer_id': ...,
    'signed_by': ...,
    'invite_id': ...,           # REMOVE
    'invite_signature': ...,    # REMOVE
    'response_transit_key': ...,
    ...
}
```

New structure:
```python
{
    'type': 'sync_connect',
    'transit_key': ...,         # Symmetric key for reverse direction
    'signed_by': ...,           # Either invite_id OR peer_shared_id
    'sig': ...,                 # Single signature field
    'created_at': ...,
    'ttl_ms': ...
}
```

### 2. Create `sync_connect_ack` event type
New event:
```python
{
    'type': 'sync_connect_ack',
    'transit_key': ...,         # Symmetric key for reverse direction
    'created_at': ...,
    'ttl_ms': ...
}
# No signature - implicit auth via decryption
```

### 3. Update `sync_connect.send_connect()`
- Build simplified event structure
- Single `signed_by` field pointing to invite_id or peer_shared_id
- Single `sig` field

### 4. Update `sync_connect.project()`
- On valid connect receipt, send `sync_connect_ack` back
- Ack is wrapped to sender's `transit_key` (from the connect)
- Store connection with their transit_key for sending to them

### 5. Create `sync_connect_ack.project()` (or handle in sync_connect.py)
- On ack receipt, no signature verification needed
- Just extract `transit_key` and store for sending to them
- Implicit auth: we could decrypt it, so it came from who we sent to

### 6. Update connection table schema
```sql
CREATE TABLE connections (
    peer_shared_id TEXT,        -- May be empty for bootstrap
    their_transit_key BLOB,     -- Key to send TO them
    origin_ip TEXT,
    origin_port INTEGER,
    last_seen_ms INTEGER,
    ttl_ms INTEGER,
    PRIMARY KEY (peer_shared_id)
);
```

### 7. Update `sync.send_requests()` to use new connection model
- Query connections table
- Use `their_transit_key` for wrapping

### 8. Update tests

## Key Insight

Each side uses the key **the other side provided** to send to them:
- Alice → Bob: wrap with `bob_transit_key` (from Bob's connect)
- Bob → Alice: wrap with `alice_transit_key` (from Alice's ack)

"Here's the key to my mailbox."

## Files to Modify

- `events/network/sync_connect.py`
- `events/network/sync_connect.sql` (schema)
- `events/network/sync.py`
- `tests/` - connection and sync tests
