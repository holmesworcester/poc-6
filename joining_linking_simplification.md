**Goal**
- Simplify joining, device linking, and bootstrap so the flow is easy to reason about, reduces special cases, and aligns with the ideal protocol direction.

**Rough Ideas**

Sources of complexity:

1. send bootstrap must continue until we get a response, and that only means that one peer knows we joined! now we have more states and cases. and what happens if that peer goes offline forever before sharing with others? *simplification:* send invite proof with signed sync requests from the beginning and forever.
1. separate invite types for users and linking.
1. "foreign local deps" complicates things. Instead, just project all local-only events first and don't treat them as deps. 
1. sync is just one way-- this means we have to do stuff like send bootstrap events. if there was some connection step to establish a secret an address on both sides and a secret for communication it might be better. 
1. it would be nice if we could keep the complexity of forward secrecy out of the invite/join process, but I'm not sure if this is desirable.
1. the first user is created in a different way than all subsequent users. can we converge these? 

*First: separate establishing a transit_secret/address/port pairing from the sync request and response*  

1. peers periodically send sync_connect events to peers they don't have active connections with--sealed to a prekey they have for each peer
1. sync_connect events include user_id, peer_id, and secret, signed by peer and invite private if we have an unexpired invite. 
1. sync_connect events also include proof of membership (signed by peer or invite private)
1. sync_connect events are projected locally before sending to sync_connection_attempts table with user, peer, secret, dest IP / port, ttl
1. these are routed by prekey_id, unwrapped, and projected to sync_connections table with user (if we have it), peer, secret, origin IP / port, ttl

This way, anything we received wrapped to a given secret, we know what address to respond to, and we know they have that secret. (We can look it up in sync_connection attempts)

*Then: decouple sync from complex dependencies. This way a new peer and an existing peer who knows of the invite can converge on state independent of that state*

1. peers periodically send sync requests to any active connections in sync_connections table 
1. sync requests are transit wrapped / routed like all normal data. (only connection events are sealed to prekeys)
1. invite_accepted event projects address and address prekey to invite_accepted table 
1. sync_connect.all checks this too and sends to it during normal operation
1. once existing peer syncs from joining peer, it starts sending connects to peer.  
1. sync_connect event is ephemeral and not saved or shared with others 

*Then: decouple invite validation from user/link validation*

1. inviter makes invite event, either for linking peers to a user or inviting new users. we validate this accordingly (invite can only specify that it's for new users if it's signed by admin)
1. new user device makes peer, peer_shared, user events
1. new linked device user makes peer, peer_shared
1. both invite_proof referring to peer_shared and user events. only valid for new users if the ivnite event specifies this. 
1. invite_proof projects the user or the linked device (and it depends on the invite)

main idea: having the invite_proof event depend on the user (which depends on the peer) is a good idea. that way we don't have local deps. It also has to depend on the invite event, so we need that event id at least. If these need to be projected (if user needs to see that they joined e.g.) *before* we get the invite link back, that introduces complexity so let's drop it.  

*Then: project all data from invite_accepted to its own table*

1. all info from the invite link is projected to an invite_accepted table (safe, just one per peer)
1. the recurring sync request can send requests to this address, prekey like anything else; it checks it in a separate lookup
1. group_key_shared projector checks this table for keys, to decrypt group keys wrapped to the invite prekey.

*Fourth: invite_proof projection is the same for all users; no local exceptions!*

1. invite_accepted projects the community creator 
1. once we have synced enough data to validate invite proof, we project invite_proof the same locally as we would if we received it for another user.
1. project anything not in the user and peer events (not sure what there might be) to an invite_proofs table

Main ideas: make invite proof projection the same everywhere 


**End-to-End Flow (Summary)**
- New user join
  - Joiner scans invite link → creates `peer` + `peer_shared` → creates local `invite_accepted` (stores invite private key into `group_prekeys` + metadata row into `invite_accepteds`) → creates `user` (joiner-authored) → creates `invite_proof` (shareable) → starts sending `sync` requests.
  - Existing peers validate `invite_proof` and project `user`/membership consistently.
- Device link
  - Joiner (new device) scans link → creates `peer` + `peer_shared` → creates local `link_invite_accepted` (optional metadata projection; still stores private key into `group_prekeys`) → creates `link` (joiner-authored) → creates `invite_proof` (mode=link) → starts sending `sync` requests.
- Two-way sync without prior peer knowledge
  - `sync` requests are signed by the peer; optionally include `invite_id` + `invite_signature` to authenticate even when the requester’s `peer_shared` is not yet known.
  - Receivers respond and also send a single reflected `sync` back wrapped to the requester’s provided `response_transit_key` to seed immediate two-way.
- Bootstrap routing/decrypt
  - Bootstrap/GKS blobs sealed to the invite prekey route via `group_prekeys` (invite prekey id) and decrypt using the local key inserted by `invite_accepted`.

---

## New/Extended Event Types and Tables

Below are complete shapes and SQL schemas for review. Field names follow existing conventions. All JSON events are canonicalized and signed like existing shareable/plaintext events.

### 1) Project `invite` fully (user invites)

Store all important invite fields explicitly for lookup and debugging (do not depend on blobs during normal operation).

- Event (fields already present in current code):
  - `invite_pubkey: str`
  - `invite_prekey_id: str`
  - `network_id: str`
  - `group_id: str`
  - `channel_id: str`
  - `key_id: str`
  - `inviter_peer_shared_id: str` (a.k.a. `created_by`)
  - `inviter_transit_prekey_shared_id: str`
  - `inviter_transit_prekey_id: str`
  - `inviter_transit_prekey_public_key: str` (b64 of raw bytes)
  - `created_at: int`

- Projection/validation additions:
  - If not bootstrap (URL import), verify signature as today.
  - Enforce inviter authorization for "new user" invites per your rules (admin check).

- Table: replace/extend `invites` to explicitly store all fields (peer-subjective):

```
CREATE TABLE IF NOT EXISTS invites (
    invite_id TEXT NOT NULL,
    invite_pubkey TEXT NOT NULL,
    invite_prekey_id TEXT NOT NULL,
    network_id TEXT,
    group_id TEXT NOT NULL,
    channel_id TEXT,
    key_id TEXT,
    inviter_peer_shared_id TEXT NOT NULL,
    inviter_transit_prekey_shared_id TEXT,
    inviter_transit_prekey_id TEXT,
    inviter_transit_prekey_public_key BLOB,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (invite_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_invites_prekey
ON invites(invite_prekey_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_invites_inviter
ON invites(inviter_peer_shared_id, recorded_by);
```

### 1b) Project `link_invite` fully (device linking)

Store all important link-invite fields explicitly for lookup and debugging.

- Event (fields already present in current code):
  - `link_pubkey: str`
  - `link_prekey_id: str`
  - `user_id: str`
  - `network_id: str`
  - `channel_id: str`
  - `key_id: str`
  - `existing_peer_shared_id: str` (a.k.a. `created_by`)
  - `existing_transit_prekey_shared_id: str`
  - `existing_transit_prekey_id: str`
  - `existing_transit_prekey_public_key: str` (b64 of raw bytes)
  - `created_at: int`

- Table: replace/extend `link_invites` to explicitly store all fields (peer-subjective):

```
CREATE TABLE IF NOT EXISTS link_invites (
    link_invite_id TEXT NOT NULL,
    link_pubkey TEXT NOT NULL,
    link_prekey_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    network_id TEXT,
    channel_id TEXT,
    key_id TEXT,
    existing_peer_shared_id TEXT NOT NULL,
    existing_transit_prekey_shared_id TEXT,
    existing_transit_prekey_id TEXT,
    existing_transit_prekey_public_key BLOB,
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (link_invite_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_link_invites_prekey
ON link_invites(link_prekey_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_link_invites_existing
ON link_invites(existing_peer_shared_id, recorded_by);

CREATE INDEX IF NOT EXISTS idx_link_invites_user
ON link_invites(user_id, recorded_by);
```

### 2) Local-only `invite_accepted` metadata table

Keep the existing behavior (store invite private key into `group_prekeys`). Add a peer-subjective table to retain OOB routing/contact data for normal operation.

- New table: `invite_accepteds`

```
-- Peer-subjective snapshot of invite OOB data
CREATE TABLE IF NOT EXISTS invite_accepteds (
    invite_id TEXT NOT NULL,
    inviter_peer_shared_id TEXT NOT NULL,
    addr TEXT,                         -- e.g., "127.0.0.1:6100" (optional)
    inviter_transit_prekey_id TEXT,    -- base64 id string (optional)
    inviter_transit_prekey_public_key BLOB, -- raw bytes (optional)
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (invite_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_invite_accepteds_recorded_by
ON invite_accepteds(recorded_by);
```

- `invite_accepted.project()` additions:
  - Read inviter metadata from the stored invite blob (already stored during join) and insert/update `invite_accepteds`.
  - Do NOT move the private key; keep using `group_prekeys` for decrypt.

### 3) New `invite_proof` event (shareable)

Joiner-authored proof that binds their identity to the invite, enabling consistent projection by everyone.

- Event: `type: 'invite_proof'` (signed by joiner peer private key)

Fields:
- Common
  - `invite_id: str` (event id of the inviter’s invite)
  - `mode: 'user' | 'link'`
  - `joiner_peer_shared_id: str` (the joiner’s peer_shared id)
  - `invite_signature: str` (Ed25519 signature over canonical message, created using invite private key)
  - `created_by: str` (joiner_peer_shared_id)
  - `created_at: int`
- User mode only
  - `user_id: str` (the joiner-authored user event id)
- Link mode only
  - `link_user_id: str` (the target user id the device is linking to)

Signature rules:
- `invite_signature` signs the canonical JSON of:
  - User mode: `{invite_id, joiner_peer_shared_id, user_id}`
  - Link mode: `{invite_id, joiner_peer_shared_id, link_user_id}`
  (Canonicalization matches existing `crypto.canonicalize_json`.)

Projection (peer-subjective):
- Load inviter’s invite row via `invite_id` from the `invites` table; read `invite_pubkey` (and `network_id` if needed).
- Verify `invite_signature` with `invite_pubkey`.
- Enforce invite kind/authorization (admin-required for new-user invites).
- Mode=user:
  - Mark `user_id` valid for this peer; ensure/insert `users` + `group_members` consistent with current `user.project()` behavior.
- Mode=link:
  - Insert into `linked_peers` for `(link_id, user_id, peer_id)` inferred from the joiner’s `peer_shared_id` and `link_user_id`.
  - Mirror a `users` row for the new device (same `user_id`, different `peer_id`).
- Mark `valid_events` for the `invite_proof` itself.

- Table (optional helper index for queries/testing):

```
CREATE TABLE IF NOT EXISTS invite_proofs (
    invite_proof_id TEXT NOT NULL,
    invite_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('user','link')),
    joiner_peer_shared_id TEXT NOT NULL,
    user_id TEXT,            -- present for mode='user'
    link_user_id TEXT,       -- present for mode='link'
    created_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (invite_proof_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_invite_proofs_invite
ON invite_proofs(invite_id, recorded_by);
```

Notes: You can synthesize this row from the event during projection; storing it helps with debugging and queries.

### 4) Extended `sync` request (ephemeral)

Augment `sync` with invite-based authentication so responders can accept a requester before they’ve projected the requester’s `peer_shared`.

Added fields (optional):
- `invite_id: str`
- `invite_signature: str` (signature by invite private key over canonical message, e.g., `{created_by, invite_id, window_id}`)

Acceptance logic in projection:
- Accept if ANY:
  - `verify_signed_by_peer_shared(sync)` passes, OR
  - `invite_signature` verifies with `invite_pubkey(invite_id)`.
- Rate-limit reflected syncs to one per pair until a valid request is observed from the other side to avoid ping-pong.

No table change (still ephemeral); keep using `bootstrap_completers` and `network_joiners` as today.

### 5) Optional `sync_connect` (ephemeral) and `sync_connections` (ephemeral table)

If you prefer an explicit connection primitive (can be added later; reflected sync above already achieves two-way), here’s the schema.

Event: `type: 'sync_connect'` (ephemeral, sealed to recipient’s prekey)
- Fields:
  - `created_by: str` (joiner_peer_shared_id)
  - `invite_id: str` and `invite_signature: str` OR rely on peer signature if known
  - `addr: str` (e.g., `127.0.0.1:6100`)
  - `response_transit_key_id: str` + `response_transit_key: str` (key material owned by sender)
  - `ttl_ms: int` (optional; can be omitted in first pass)
  - `created_at: int`

Projection (ephemeral): insert/update `sync_connections`.

Table: `sync_connections`
```
CREATE TABLE IF NOT EXISTS sync_connections (
    peer_shared_id TEXT NOT NULL,          -- remote peer
    response_transit_key_id TEXT NOT NULL, -- how to send back
    addr TEXT,
    ttl_ms INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,             -- local peer
    PRIMARY KEY (peer_shared_id, recorded_by)
);

CREATE INDEX IF NOT EXISTS idx_sync_connections_recorded_by
ON sync_connections(recorded_by);
```

Senders may consult this table before choosing targets for `send_request_to_all()`.

---

## Validation, Routing, and Decrypt Rules

- Bootstrap routing
  - Route transit blobs sealed to invite prekeys by checking `group_prekeys.prekey_id` in `route_blob_to_peers()`.
- Bootstrap decrypt
  - Allow `crypto.unwrap_transit()` to look up invite-prekey private keys in `group_prekeys` (asymmetric) for bootstrap-wrapped blobs.
- Proof projection
  - Do not special-case local projection; the same `invite_proof` rules apply whether received for self or others.

---

## Backward Compatibility

- Keep existing `invite_accepted`/`link_invite_accepted` behavior (store invite/link private key into `group_prekeys`).
- New tables (`invite_accepteds`, `invite_proofs`, optional `sync_connections`) are additive and do not break existing flows.

---

## Security Notes

- Invite-based auth permits an invite holder to elicit responses; teams may require approvals/extra checks for sensitive areas.
- All added signatures use existing Ed25519 and canonical JSON. Ensure signature messages only include necessary fields to avoid replay across contexts.
