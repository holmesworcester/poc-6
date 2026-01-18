# Plan: Merge Master into proj-v2-base

## Summary

Master has significant changes that need to be merged into our v2 projector branch. The key changes on master are:

1. **Address-based transport layer** - New `core/transport.py` replacing NAT simulator
2. **Trust anchor enforcement** - Network events rejected unless in `trust_anchors`
3. **Negentropy optimization removed** - Always sync all connections
4. **Address discovery** - `packet_metadata` staging table + address columns in connections

## Changes on Master (not in proj-v2-base)

### New Files
| File | Purpose |
|------|---------|
| `core/transport.py` | Simple address-based transport with in-memory queues |
| `core/udp.py` | Real UDP socket support |
| `events/network/packet_metadata.sql` | Staging table for packet source addresses |
| `docs/tla/*` | TLA+ models for verification |
| `docs/verified-implementation-path.md` | Implementation verification doc |

### Modified Files with Logical Changes

#### `events/identity/network.py`
- `create()` now adds trust_anchors entry BEFORE storing event (so projection passes)
- `project()` has trust anchor enforcement (duplicate of what's in recorded.py)

**⚠️ FIX NEEDED**: The pre-insertion in `network.create()` is wrong. Trust anchors should come from `invite_accepted` for ALL cases:

- **Creator**: Self-invites via `peer_shared.join()` → creates `invite_accepted` with `network_id` → trust anchor inserted
- **Joiner**: Accepts invite → `invite_accepted` with `network_id` → trust anchor inserted

This is a uniform model: everyone (including creators) goes through `invite_accepted`. The creator "invites themselves" - see `peer_shared.join()` line 599-616 which creates `invite_accepted` with `network_id` in the link data.

The pre-insertion in `network.create()` bypasses this proper flow. Remove it during merge.

Also remove the `is_creator = not has_invite_accepted` check from `recorded.py` - it's wrong. Creators DO have an `invite_accepted` (they invite themselves).

#### `events/network/connection.py` (on master) / `connection_request.py` (our branch)
- New `get_address_for_peer()` function
- `_project_request` looks up from_addr from `packet_metadata` staging table
- `_project_ack` stores address in connections table
- Uses transport layer instead of `queues.incoming`
- Removed `pending_connection_requests` usage in request projection

#### `events/network/recorded.py`
- Trust anchor enforcement for network events (checks `trust_anchors`, `networks`, `valid_events`, `invite_accepteds`)

#### `events/network/negentropy.py`
- Removed "skip if our root unchanged" optimization
- Now always syncs all connections to pull remote events

#### `events/network/sync.py`
- New `store_packet_from_addr()` function
- New `store_incoming()` unified receive function with address metadata

#### `core/jobs.py`
- Renamed `SyncReceiveJob` to `ReceiveJob`
- Uses transport layer: `transport.loopback_transfer()` or `transport.udp_transfer()`
- Calls `sync.store_incoming()` instead of `sync.receive()`

### Schema Changes

#### `events/network/connection.sql`
Master adds:
```sql
from_addr_ip TEXT,
from_addr_port INTEGER,
```

Our branch has:
```sql
peer_ip TEXT,
peer_port INTEGER,
address_source TEXT,
address_learned_ms INTEGER,
```

**Decision**: Use master's simpler naming (`from_addr_ip/from_addr_port`). Our extra columns (`address_source`, `address_learned_ms`) can be dropped - they were speculative.

## Conflicts to Resolve

### 1. `core/jobs.py`
**Conflict**: We use `core/receive.py`, master uses `core/transport.py` + `sync.store_incoming()`

**Resolution**: Take master's transport layer, but keep our `core/receive.py` (add address discovery to it).

### 2. `events/network/connection.sql`
**Conflict**: Different address column names

**Resolution**: Use master's naming (`from_addr_ip`, `from_addr_port`). Drop our extra columns.

### 3. `events/network/connection_request.py` vs `connection.py`
**Conflict**: We split into `connection_request.py` + `connection_ack.py`, master keeps single `connection.py`

**Resolution**: Keep our split (cleaner separation), but port master's changes:
- Add `get_address_for_peer()` function
- Add `packet_metadata` lookup in projection
- Update address columns in connections
- Use transport layer for sending

### 4. `events/network/sync.py`
**Conflict**: We deleted it, master added `store_incoming()` and `store_packet_from_addr()`

**Resolution**: Don't restore sync.py - it's mostly legacy (bloom filter sync replaced by negentropy). Instead:
- Keep our `core/receive.py`
- Add `store_packet_from_addr()` to `core/receive.py` for address discovery
- Update `ReceiveJob` to use `receive.store_incoming()` instead of `sync.store_incoming()`

### 5. `tests/scenario_tests/test_multiplayer_matrix.py`
**Resolution**: Take master's test changes (they test the new transport layer)

## Merge Strategy

### Phase 1: Commit Current Changes
Before merging, commit all our v2 projector work to have a clean state.

### Phase 2: Merge Master
```bash
git merge master
```

### Phase 3: Resolve Conflicts
For each conflict file:

1. **jobs.py**: Take master's `ReceiveJob` implementation
2. **connection.sql**: Use master's address columns
3. **connection_request.py**:
   - Keep our file structure (request/ack split)
   - Port master's `get_address_for_peer()`
   - Port master's `packet_metadata` lookup
   - Port transport layer usage
4. **sync.py**: Don't restore - add address discovery to `core/receive.py` instead
5. **test_multiplayer_matrix.py**: Take master's version

### Phase 4: Integration
- Keep `core/receive.py`, add `store_packet_from_addr()` for address discovery
- Update `ReceiveJob` to use transport layer + `receive.store_incoming()`
- **Fix trust anchor model** (uniform for all: creator and joiner):
  - Remove pre-insertion from `network.create()` - wrong, bypasses proper flow
  - Remove `is_creator = not has_invite_accepted` check from `recorded.py` - wrong, creators DO have invite_accepted
  - Keep `trust_anchors` check in `recorded.py` - correct, populated by `invite_accepted.project()`
  - Remove duplicate enforcement from `network.project()` (already in recorded.py for v2 path)
- Ensure v2 projectors work with new transport layer
- Run full test suite

### Phase 5: Verify
```bash
PYTHONPATH=. pytest tests/ -v --tb=short
```

## Key Integration Points

### Trust Anchor Enforcement
Master has this in THREE places (overcomplicated):
1. `recorded.py` - for v2 path (keep, but fix the `is_creator` logic)
2. `network.py` `project()` - for legacy path (remove - we use v2)
3. `network.py` `create()` - pre-inserts trust anchor (remove - wrong model)

**Correct trust model (uniform for creator AND joiner):**
- **Creator**: Self-invites via `peer_shared.join()` → `invite_accepted` with `network_id` → trust anchor inserted
- **Joiner**: Accepts invite → `invite_accepted` with `network_id` → trust anchor inserted

Everyone goes through `invite_accepted`. The creator "invites themselves" - `new_network()` calls `peer_shared.join()` which creates `invite_accepted`.

**Fix in recorded.py:**
- Remove the `is_creator = not has_invite_accepted` check - it's wrong (creators DO have invite_accepted)
- Just check `trust_anchors` table (populated uniformly by `invite_accepted.project()`)

### Transport Layer
Master's transport layer is simpler than our NAT simulator approach:
- `transport.deliver(blob, from_addr)` - receive
- `transport.send(blob, from_addr, to_addr)` - send
- `transport.loopback_transfer()` - test mode
- `transport.udp_transfer()` - production mode

This should integrate cleanly with our v2 projectors.

### Address Discovery Flow
1. Packet arrives with source address
2. `receive.store_incoming()` stores address in `packet_metadata`
3. Connection projection reads from `packet_metadata`
4. Connection projection writes to `connections.from_addr_*`
5. Future sends use `get_address_for_peer()` to look up address

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| V2 projectors incompatible with new transport | Transport just delivers blobs - projectors unchanged |
| Trust anchor enforcement breaks existing tests | Tests should already have trust anchors set up |
| Address columns cause migration issues | SQLite ALTER TABLE ADD COLUMN is safe |

## Estimated Effort

- Phase 1 (Commit): 5 minutes
- Phase 2 (Merge): 2 minutes
- Phase 3 (Resolve): 30-45 minutes
- Phase 4 (Integration): 15-20 minutes
- Phase 5 (Verify): 10 minutes

**Total**: ~1 hour
