# Multi‑Device Sync: Per‑Local‑Peer Connection Scoping

## Summary

Problem: After linking multiple devices (multiple peers on the same physical device), each peer only sees its own messages during sync. Initial link and key sharing work (GKS + channel), but post‑link message sync fails to cross peers.

Root cause: `sync_connections` is device‑wide and keyed only by the remote `peer_shared_id`. When a device has multiple local peers, ephemeral sync requests/responses may be projected under the “wrong” local peer. Since `send_response()` selects from `shareable_events` by `can_share_peer_id = from_peer_id`, the responder may look at the wrong peer’s shareable set and omit events authored by the intended local peer.

Fix: Scope connections per local peer by adding `from_peer_id` to `sync_connections` and consistently using it end‑to‑end. Connections become between `(from_peer_id, peer_shared_id)`; sync is initiated and responded to under the correct local peer, so `shareable_events` selection matches.


## Terminology and Naming (consistent, unambiguous)

- Local Peer ID: `peer_id`
  - Subjective, device‑local identity (appears in `recorded_by`/scoped tables).
  - Owns local private keys and subjective state.

- Public Peer ID: `peer_shared_id`
  - Shareable public identity (appears in shareable events, remote parties learn this).

- Connection Owner: `from_peer_id`
  - The local `peer_id` that owns/establishes a connection to a particular remote `peer_shared_id`.

- Remote Peer (public): `peer_shared_id`
  - The other side of the connection.

- Requester (in sync requests): `requester_peer_shared_id`
  - The `created_by` value in the sync request (requester’s public identity).

These names must be used consistently in code, SQL, and logs:
- “from” always refers to a local peer (`from_peer_id` = local owner of the connection).
- “to” or “remote” always refers to a public peer (`to_peer_shared_id` / `peer_shared_id`).


## Planned Changes

### 1) Schema: `sync_connections`

Add `from_peer_id` and change the key to be per local peer.

Current (conceptual):
```
sync_connections(
  peer_shared_id TEXT PRIMARY KEY,
  response_transit_key_id TEXT,
  response_transit_key BLOB,
  address TEXT,
  port INTEGER,
  invite_id TEXT,
  last_seen_ms INTEGER,
  ttl_ms INTEGER
)
```

Proposed:
```
sync_connections(
  from_peer_id TEXT NOT NULL,        -- local peer owning this connection
  peer_shared_id TEXT NOT NULL,      -- remote public peer
  response_transit_key_id TEXT NOT NULL,
  response_transit_key BLOB NOT NULL,
  address TEXT,
  port INTEGER,
  invite_id TEXT,
  last_seen_ms INTEGER NOT NULL,
  ttl_ms INTEGER NOT NULL,
  PRIMARY KEY (from_peer_id, peer_shared_id)
)

-- Recommended indexes
CREATE INDEX IF NOT EXISTS idx_sync_connections_active
  ON sync_connections(from_peer_id, last_seen_ms, ttl_ms);
```

Rationale:
- Makes connections explicit per local peer.
- Enables querying only the connections relevant to a given `from_peer_id` during sync.

Migration note:
- Table is device‑wide and ephemeral (5‑minute TTL in current code). For dev/tests, a recreate is fine. For prod, a simple “recreate and repopulate” or “copy into new schema” migration will work; worst case, allow connections to expire and be re‑established.


### 2) `sync_connect.project()`

- When projecting a received `sync_connect`, store the connection as `(from_peer_id = recorded_by, peer_shared_id = sender’s peer_shared)`.
- Upsert using the new composite primary key `(from_peer_id, peer_shared_id)`.

Why: `recorded_by` is the local peer that successfully decrypted the connect; that is the proper `from_peer_id` (connection owner) on this device.


### 3) `sync.send_requests()`

- Currently queries all device‑wide connections, then sends requests. Change to query only connections for the specific `from_peer_id` being scheduled.
- For each local peer (`from_peer_id`), iterate its active connections and call `send_request(to_peer_shared_id, from_peer_id, from_peer_shared_id, ...)`.

Why: Ensures sync runs under the same local peer identity that owns the connection and the relevant `shareable_events`.


### 4) `sync.project()` (handling sync requests)

- Keep the existing signature verification and window/bloom handling.
- When checking whether to accept a request (for bootstrap), look for an active connection row using `(from_peer_id = recorded_by, peer_shared_id = requester_peer_shared_id, last_seen_ms + ttl_ms > recorded_at)`.
- If no valid `peer_shared` event for the requester and no active connection, reject; otherwise accept.

Why: Maintain a single policy gate: either we’ve synced their public identity, or we have a live authenticated connection with them.


### 5) `send_response()`

- No logic change, but it will now be executed under the correct `from_peer_id` consistently.
- Its query stays: `SELECT event_id FROM shareable_events WHERE can_share_peer_id = from_peer_id AND window_id BETWEEN ...` which is the intended scope.


## Data Flow Walkthrough (happy path)

1. Device A (local `from_peer_id = A1`) receives `sync_connect` from Device B (`peer_shared_id = Bx`). Project stores row: `(from_peer_id=A1, peer_shared_id=Bx, key_id, key, last_seen, ttl)`.
2. Tick runs `sync.send_requests()` for A1. It queries only A1’s connections and sends a signed/bloomed request to Bx.
3. Device B projects the sync request under a specific local peer (say, `from_peer_id = B7`) via routing. `sync.project()` accepts because either B has A’s `peer_shared` or an active connection `(from_peer_id=B7, peer_shared_id=Ax)`.
4. `send_response()` on B runs under `from_peer_id = B7` and selects B7’s shareable events to send to A.
5. Device A unwraps and projects, updating A1’s state.

Result: Per‑peer subjective views are preserved and cross‑device messages flow correctly.


## Backward Compatibility and Migration

- Dev/test: drop/recreate `sync_connections` with new schema.
- Prod: because connections are short‑lived TTL entries, a one‑time migration that clears old entries is acceptable (they’ll be rebuilt by `sync_connect`). If needed, add a temporary acceptance clause in `sync.project()` that treats legacy rows (without `from_peer_id`) as device‑wide until expired.


## Test Plan

- Re‑run multi‑device and linking scenarios:
  - `tests/scenario_tests/test_multi_device_linking.py`
  - `tests/scenario_tests/test_linked_device_messaging.py`
  - `tests/scenario_tests/test_link_device_pre_existing_groups.py`
  - `tests/scenario_tests/test_link_device_new_groups.py` (if present)
- Verify bidirectional message delivery and historical sync.
- Sanity on single‑peer sync and three‑peer link (`test_sync_three_players.py`) to ensure no regressions.


## Risks and Mitigations

- Schema change risk: kept minimal and localized; connections are ephemeral.
- Performance: add index on `(from_peer_id, last_seen_ms, ttl_ms)` to query active connections per peer efficiently.
- Naming/consistency: this doc standardizes the names. Code, SQL, logs must use the same terms (`from_peer_id`, `peer_shared_id`, `requester_peer_shared_id`).


## Out of Scope (for this change)

- Changing routing logic for transit blobs.
- Altering bloom/window sizing.
- Unifying legacy link_invite format (already partially handled elsewhere).


## Rollout Steps

1. Update `sync_connect.sql` and `sync_connect.project()` to include `from_peer_id` and composite PK.
2. Update `sync.send_requests()` to filter by `from_peer_id`.
3. Tighten `sync.project()` acceptance to check active connection with `(from_peer_id=recorded_by, peer_shared_id=requester_peer_shared_id)` in addition to known `peer_shared`.
4. Clean up any legacy connection checks and ensure logs reflect the new names.
5. Run scenario tests listed above.


## Acceptance Criteria

- After linking two devices, each peer sees messages from the other peer in the same channel.
- Pre‑existing and post‑link groups synchronize correctly.
- No regressions in single‑peer or three‑peer sync tests.

