# Event‑Centric Full Protocol Implementation Plan

This plan translates the “everything is an event” redesign into a concrete, end‑to‑end flow for the full protocol, covering transit‑layer encryption (prekey‑sealed datagrams), event‑layer encryption (group/channel keys), blobs, and forward secrecy (rekeying). It leans on two powerful framework guarantees:

- Validated refs: referenced IDs (group_id, channel_id, message_id, user_id, prekey_id, key_id, etc.) are validated before delivery; otherwise the source event is blocked.
- Hydration: processors receive each event with a 1‑hop dependency context (the dependent events themselves), alongside local DB state.

The result is simple, deterministic processors that only transform events and emit new events; DB effects are expressed as `db.delta/*` and applied by a single delta applier saga.

## Naming, IDs, and Hydration

- Event types: human‑readable `domain.verb` names and stable wire codes (see Appendix A in the spec). This plan references known codes where helpful.
- IDs: use the spec’s IDs and naming consistently: `group_id`, `channel_id`, `user_id`, `peer_pk` (pubkey), `peer_id` (hash of peer event), `prekey_id`, `key_id`, `message_id`, `blob_id`, `slice_no`.
- Hydration: each processor declares `DEPS = ["group_id", "channel_id", ...]`; the framework injects a map of those events: `deps = { field_name: hydrated_event }`.
- Blocking: if any dependency is missing/not-validated, the framework blocks the source event and publishes it later when deps validate.

## Event Catalog (by domain, with dependencies)

Note: codes are from Appendix A of the draft; only key dependencies are listed (hydration is 1 hop).

### Identity & Network

- LOCAL‑ONLY‑peer (aka LOCAL‑ONLY‑identity): device keypair for a network. No deps (local‑only).
- LOCAL‑ONLY‑network: binds a local network entry to a `group_id`. Dep: group.
- address (0x0C): `transport`, `addr`, `port`. No explicit deps. Implicit hydration includes signer mapping `{ signer_peer_pk → signer_user_id }` derived from link/user events. Address is network‑agnostic; network binding comes from the transit layer.
- intro (0x0B): `address1_id`, `address2_id`. Deps: address1, address2.
- invite (0x0D): `invite_pk`, `max_join`, `expiry_ms`, `network_id`. Dep: group/network.
- user (0x0E): `pake_proof`, `network_id`. Dep: group/network.
- link‑invite (0x0F): `invite_pk`, `max_join`, `expiry_ms`, `user_id`, `network_id`. Deps: user, network.
- link (0x10): `pake_proof`, `user_id`, `network_id`. Deps: user, network.
- remove‑peer (0x11): `peer_id`. No deps.
- remove‑user (0x12): `user_id`. Dep: user.

 

### Framework/Infra Events (Transit, Parsing, Encrypted Payloads)

We explicitly model transport and envelope processing as events so blocking and hydration apply uniformly.

- LOCAL‑ONLY‑incoming: raw datagram `{ transport, origin, received_at_ms, bytes }`.
- transit.prekey.opened: result of opening a datagram sealed to our short‑lived transit prekey. Fields: `{ prekey_id, network_id, peer_remote_pk, reply_path, payloads[] }`. The transit saga maintains local maps: `prekey_id → (network_id, peer_remote_pk)` and `prekey_secret[prekey_id]` to attribute packets and open them. If the needed prekey is absent, this event blocks until the corresponding `prekey` is available (or the secret is installed locally).
- event.wire: one 512‑byte event extracted from a payload. Fields: `{ wire_id, type_code, header_fields, body_bytes }`. The canonical `wire_id = BLAKE2b‑128(512B event)` over the event‑layer encrypted form (transit wrapper excluded). All references (message_id, group_id, etc.) use this id so peers that can’t decrypt still correlate deletions and updates.
- event.encrypted: for types with event‑layer encryption, provides `{ wire_id, type_code, created_at_ms, ttl_ms, signer_peer_pk, key_id, cipher, hmac_hint }`. This blocks until the referenced `key` is present.
- event.decrypted: upon successful open, yields `{ id=wire_id, type, payload_plaintext, signer_peer_pk }` used by validators/projectors.
- LOCAL‑ONLY‑outgoing: fully prepared transit‑sealed payloads `{ peer_remote_pk, network_id, bytes, due_ms }` scheduled by sync/transport sagas.

### Groups & Membership

- group (0x14): `user_id`, `group_name`. Dep: user.
- fixed‑group (0x16): `num_members`, `user_ids[]` (≤20, sorted). Deps: each user.
- grant (0x17): `group_id`, `user_id`. Deps: group, user.
- update‑group‑name (0x15): `group_id`, `new_name`. Dep: group.

### Channels & Messaging

- channel (0x01): `group_id|fixed_group_id`, `channel_name`, `disappearing_time_ms`. Dep: group/fixed‑group.
- channel‑update (0x1F): `channel_id`, `new_channel_name`, `new_disappearing_time_ms`, `global_count`. Dep: channel.
- delete‑channel (0x06): `channel_id`. Dep: channel.
- message (0x00): `channel_id`, `text`. Dep: channel.
- update (0x02): `event_id`, `global_count`, `update_code`, `user_id`, `body`. Deps: target event; user.
  - Notable bodies: `edit-message-text`, `add-attachment`, `add-reaction`, `remove-reaction`, `update-username`, `update-profile-image`, `add-prekey`.
- delete‑message (0x05): `message_id`. Dep: message.
- seen (0x21): `channel_id`, `viewed_at_ms`, `message_id`. Deps: channel, message.

### Event‑Layer Encryption & Rekeying

- prekey (0x19): `group_id`, `channel_id`, `prekey_pub`, `eol_ms`. Deps: group/channel.
- key (0x18): `peer_pk`, `count`, `created_ms`, `ttl_ms`, `tagId`, `prekey_id`, `sealed_key`. Dep: prekey.
- rekey (0x04): `original_event_id`, `new_key_id`, `new_ciphertext`. Deps: original event, key.

Encrypted domain types
- All encrypted types (e.g., `message`, `channel`, `update`, `delete-*`, `seen`, `grant`, `group`, etc. where Appendix A marks “Yes”) include a `key_id` in their plaintext header and declare a dependency on that `key`. This lets the framework block/unblock on `key_id` and hydrate the correct key material deterministically.

### Blobs

- update:add‑attachment (update body 0x01): `blob_id`, `blob_bytes`, `nonce_prefix`, `enc_key`, `root_hash`. Dep: message.
- slice (0x03): `blob_id`, `slice_no`, `nonce24`, `ciphertext`, `poly_tag`. Dep: blob root (via `blob_id` on the add‑attachment update).

### Sync & Transit

- sync (0x07): `window`, `bloom_bits`. Framework hydration can include `window_events` (summaries) matching the window selector so the responder applies the bloom without bespoke DB paging.

- sync‑auth (0x08): `window`, `bloom_bits`, `limit`.
- sync‑lazy (0x09): `cursor`, `bloom_bits`, `limit`, `channel_id`. Dep: channel. Hydration can provide `channel_window_events` (summaries before `cursor`, up to `limit`).
- sync‑blob (0x0A): `blob_id`, `window`, `bloom_bits`, `limit`. Dep: blob root.

### Push (optional)

- push‑server (0x1A): `user_id`, `security_settings`. Dep: user.
- push‑register (0x1B): `token`, `ttl_ms`. No deps.
- push‑mute (0x1C), push‑unmute (0x1D): `channel_id`. Dep: channel.
- mute‑channel (0x1E): `channel_id`, `mute_flag`. Dep: channel.

## Validation Rules (selected)

- group: signer is bootstrap admin (creator’s user). Hydration supplies `user` to confirm linkage.
- grant: signer is admin of `group_id`. Hydration gives `group`; admin set derives from grants DB.
- channel: signer is admin of group/fixed‑group. Hydration gives group/fixed‑group.
- channel‑update / delete‑channel: signer is admin; for fixed‑group channels, allow any member to delete.
- message: signer is member; channel exists; created_at/TTL sane; payload well‑formed.
- update:add‑attachment: signer allowed to attach; reassembled blob matches `root_hash` before projection.
- seen: signer is channel member; `viewed_at_ms ≥ message.created_at_ms`.
- prekey: scope matches group/channel; `eol_ms` in future.
- key: `prekey_id` exists; `sealed_key` unwraps to group secret under recipient’s prekey; TTL sane; tagId matches ACL.
- rekey: decrypt both original and `new_ciphertext` to identical plaintext; `new_key_id.TTL > original_event.TTL`; nonce deterministic per spec.
- intro: both address refs valid; can be fast‑path.

## Deletion Policy and Enforcement

- Convergent record: domain events from removed users/peers remain valid; we do not drop them at the domain layer.
- Transit enforcement: the transit processor rejects datagrams sealed to/from removed peers (based on hydrated `remove-*` events and local allowlist).
- Projection enforcement: deletions are typed events (e.g., `delete-message`, `delete-channel`) that project to DB tombstones/TTL. Validators consult deletion state to no‑op subsequent edits.
- Optional guardrails: dispatcher can annotate hydrated deps with `deleted=true`; validators can fast‑fail or issue diagnostics.

 

## Event Chains (ASCII “pictures”)

 

Message creation to persisted row

```
command.message/create
  └─ message (0x00, encrypted) ──► crypto.event.decrypt ─► message.plain
       └─ validate.message (authz, channel membership)
           └─ project.message ──► db.delta/insert(messages, {event_id, channel_id, text, author, ts})
```

Attachment flow

```
update:add-attachment (blob_id, bytes, nonce_prefix, enc_key, root_hash) ─┐
slice(0..N) (ciphertext chunks under enc_key, merkle root = root_hash)    │
  └─ blob.manager tracks wanted slices & reassembly                       ─┴─► when all slices present and root_hash matches:
        └─ project.attachment ─► db.delta/insert(attachments)
```

Event‑layer encryption issuance and use

```
prekey (group/channel scoped) ─► key (sealed group secret G to recipient prekey)
  └─ sender caches G under tagId
      └─ any encrypted event (message/channel/update/...) uses G with AEAD
          └─ receiver selects candidates by TTL+tagId → decrypt → verify sig → validate
```

Rekeying for forward secrecy

```
purge set (keys, prekeys) detected ─► rekey.plan
  └─ for each affected event Ei:
        choose clean new_key Kj (TTL just > Ei.TTL)
        new_ciphertext = aead_seal(plaintext(Ei), nonce=H(Ei.id ∥ Kj.id), key=Kj)
        emit rekey(Ei.id, Kj.id, new_ciphertext)
  └─ rekey.validate: open(Ei), open(new_ciphertext) → identical? yes
      └─ project.rekey ─► db.delta/update(events, where={id:Ei.id}, set={ciphertext:new_ciphertext, key_id:Kj.id})
      └─ purge old keys/prekeys (db.delta/delete)
```

Transit handshake and sync

```
rx.datagram (LOCAL-ONLY-incoming) ─► transit.noise.open(IKpsk2, short-lived prekeys)
  └─ yields {peer, network} + wrapped event bytes
      └─ event.parse → id (wire form), type, header
          └─ crypto.event.decrypt (if encrypted type) → plaintext payload → sig.verify
              └─ validate.* (hydrated ids) → project.* → db.delta/*
```

## Routing Model and Sagas (subscriptions → outputs)

Dispatch
- Each saga declares `SUBSCRIBE` as a filter (e.g., `{"types": ["event.wire", "message", "update"]}`) and a stable `SAGA_NAME`.
- Before invoking a saga, the dispatcher hydrates declared `DEPS` for that event type (from a registry) and passes them as context.
- Exactly‑once per saga is enforced via `(saga_name, event_id)` in `saga_applied`.

Core processors
- transit.inbound: `LOCAL‑ONLY‑incoming` → `transit.prekey.opened`
- event.parse: `transit.prekey.opened` → `event.wire`
- crypto.event.decrypt: `event.wire` (encrypted types) → `event.encrypted` → `event.decrypted`
- validate.*: `event.decrypted` → domain.valid (or error)
- project.*: domain.valid → `db.delta/insert|update|delete`
- delta.apply: `db.delta/*` → SQL changes (single‑writer)

Support processors
- blob.manager: `update:add-attachment`, `slice`, `sync-blob` → wanted‑slices (LOCAL‑ONLY) and `db.delta/*` when complete
- rekey.planner: key/prekey expiry/removal → `rekey`
- rekey.projector: `rekey` → `db.delta/update` (replace ciphertext) + `db.delta/delete` (purge old)
- sync.scheduler: `address`, `intro`, last‑sync local state → `LOCAL‑ONLY‑outgoing` (sealed syncs)
- sync.responder: `sync/*` → transit‑sealed responses

Hydration keeps validators/projectors free of multi‑hop joins; only one hop is required.

## Worked Example: Invite → Join → Sync (Alice → Bob)

1) Alice creates invite
- Alice (admin) emits `invite` (0x0D) with `invite_pk`, `expiry_ms`, `network_id` (=`group_id`).
- Projector inserts invite; Alice shares out‑of‑band link with Bob.

2) Bob provisions identity and joins
- Bob scans link, creates LOCAL‑ONLY‑peer (identity) and emits `user` (0x0E) with PAKE proof + `network_id`.
- Validator checks proof against `invite`; projector inserts Bob’s `user` and associates `peer_pk → user_id`.
- Bob emits `address` (0x0C).

3) Event‑layer prekeys and keys
- Members publish `prekey` (0x19) for group/channel.
- Alice emits `key` (0x18) per recipient using their `prekey_id`, sealing group secret `G`. Projector stores key metadata; Bob opens `sealed_key` to obtain `G` and caches under `key.id`.

4) Transit delivery
- Bob emits a transit‑sealed `sync` (0x07) to Alice’s current transit prekey. Receiver opens via `transit.prekey.opened {network_id, peer_remote_pk}` and extracts 512‑byte events.

5) Parsing and decryption
- Dispatcher emits `event.wire` → for encrypted types, the header includes `key_id`; the framework hydrates that `key`. If the `key` is absent, the event blocks until the `key` arrives. Decrypter opens with `G(key_id)`; signature verifies; validators run.

6) Sync response and catch‑up
- On Alice: `sync.responder` uses hydrated `window_events` to apply Bob’s bloom and replies with missing events (keys, groups, channels, messages) in sealed datagrams.
- On Bob: events parse/decrypt/validate/project; history decrypts using obtained `G`; Bob’s view converges.

7) Post‑join messaging
- Bob posts `message`; event carries `key_id`; canonical id is `wire_id` of the 512‑byte ciphertext. `delete-message` references that `wire_id`, so non‑decrypting peers still delete the right row.

### Pipeline Diagrams (parallel tracks)

Legend

- seal_to transit prekey(X): encrypt datagram to short‑lived transit prekey X.
- transit.prekey.opened: receiver opened datagram with its transit prekey; attributes `network_id`, `peer_remote_pk`.
- event.wire: extracted 512‑byte event; `wire_id = BLAKE2b‑128(512B)`.
- event.encrypted: event is event‑layer encrypted; blocked until `key_id` is hydrated.
- hydrate key(key_id): framework provides referenced `key` event (and secret G) to processor.
- AEAD open with G: decrypt event‑layer using group secret G (XChaCha20‑Poly1305).
- event.decrypted: plaintext event available for validation/projection.
- validate.*: domain validation/authz.
- project.*: emit `db.delta/*` to mutate SQL.
- db.delta/*: single‑writer delta applier runs inserts/updates/deletes.
- responder applies bloom: sync responder computes diff of missing IDs within window.
- open sealed_key: unwrap group secret G from `key` using recipient’s prekey.
- cache G under key_id: store group secret locally indexed by `key_id`.
- tombstone: DB mark for deletions (soft‑delete semantics).

Create network and invite (Alice)

```
Sender (Alice)
──────────────
group (0x14, key_id A1)
↓ validate.group
↓ project.group → db.delta/insert(groups)

invite (0x0D)
↓ validate.invite
↓ project.invite → db.delta/insert(invites)

OOB link → deliver invite_pk + network_id to Bob
```

Join and address (Bob → Alice)

```
Sender (Bob)              Transit / Wire                     Receiver (Alice)
──────────────            ────────────────────────            ─────────────────────────────
user (0x0E)               datagram (prekey_id → Alice)        open with transit prekey
↓ seal_to transit prekey  ───────────────────────────────▶    ↓ event.wire
                          (no parsing here; opaque bytes)     ↓ event.decrypted
                                                               ↓ validate.user
                                                               ↓ project.user → db.delta/insert(users)

address (0x0C)            datagram (prekey_id → Alice)        open with transit prekey
↓ seal_to transit prekey  ───────────────────────────────▶    ↓ event.wire
                          (no parsing here; opaque bytes)     ↓ event.decrypted
                                                               ↓ validate.address
                                                               ↓ project.address → db.delta/insert(addresses)
```

Distribute prekeys/keys and catch‑up (Alice → Bob)

```
Sender (Alice)            Transit / Wire                     Receiver (Bob)
──────────────            ────────────────────────            ─────────────────────────────
prekey (0x19, plaintext)  datagram (prekey_id → Bob)          open with transit prekey
↓ seal_to transit prekey  ───────────────────────────────▶    ↓ event.wire
                                                               ↓ event.decrypted
                                                               ↓ validate.prekey
                                                               ↓ project.prekey → db.delta/insert(prekeys)

key (0x18, sealed_key)    datagram (prekey_id → Bob)          open with transit prekey
↓ seal_to transit prekey  ───────────────────────────────▶    ↓ event.wire
                                                               ↓ event.decrypted
                                                               ↓ open sealed_key → cache G under key_id

sync request (0x07)       datagram (prekey_id → Alice)        open with transit prekey
↓ seal_to transit prekey  ───────────────────────────────▶    ↓ event.wire
                                                               ↓ event.decrypted
                                                               ↓ responder applies bloom → returns missing events (sealed)
```

Event‑layer encrypted message and deletion (Bob → Alice)

```
Sender (Bob)                         Transit / Wire                     Receiver (Alice)
──────────────                       ────────────────────────            ─────────────────────────────────────────
message (0x00, header.key_id)
↓ AEAD encrypt with G(key_id)
↓ seal_to transit prekey(Alice)      datagram (prekey_id → Alice)        open with transit prekey
                                      ─────────────────────────────▶      ↓ event.wire
                                                                          ↓ event.encrypted (blocks if key missing)
                                                                          ↓ hydrate key(key_id)
                                                                          ↓ AEAD open with G → event.decrypted
                                                                          ↓ validate.message → project.message → db.delta/insert(messages)

delete-message (0x05, message_id)    datagram (prekey_id → Alice)        open with transit prekey
↓ AEAD encrypt with G(key_id)         ─────────────────────────────▶      ↓ event.wire
↓ seal_to transit prekey(Alice)                                          ↓ event.encrypted → decrypt via key_id
                                                                          ↓ event.decrypted
  ↓ validate.delete → db.delta/update(tombstone)
```

Sync response (Alice → Bob)

```
Sender (Alice)                         Transit / Wire                     Receiver (Bob)
──────────────                         ────────────────────────            ───────────────────────────────────────
sync response payloads (0x07/0x08/0x09)
↓ compute window/bloom diff
↓ package missing events (may include keys, groups, channels, messages)
↓ seal_to transit prekey(Bob)          datagram (prekey_id → Bob)          open with transit prekey
                                        ───────────────────────────▶        ↓ event.wire (per 512-byte event)
                                                                             ↓ event.encrypted? → hydrate key_id → AEAD open → event.decrypted
                                                                             ↓ validate.* → project.* → db.delta/*
```

Rekey sweep (purge keys/prekeys and replace ciphertext)

```
Planner (local)                        Transit / Wire                     Receivers (peers)
────────────────                        ────────────────────────           ─────────────────────────────────────────
detect purge set (keys/prekeys)
↓ choose new_key for each affected event
↓ rekey(Ei.id, new_key_id, new_ciphertext)
↓ seal_to transit prekeys (peers)       datagram(s) to peers                open with transit prekey
                                         ─────────────────────────▶         ↓ event.wire → event.encrypted → event.decrypted
                                                                             ↓ validate.rekey (open original+new; identical?)
                                                                             ↓ project.rekey → db.delta/update(events.ciphertext)

Local cleanup (sender)
↓ db.delta/delete(old_keys)
↓ db.delta/delete(old_prekeys)
```

Blob slice syncing (request and delivery)

```
Requester (Bob)                         Transit / Wire                     Responder (Alice)
────────────────                         ────────────────────────           ─────────────────────────────────────────
sync-blob (0x0A, blob_id, window, bloom)
↓ seal_to transit prekey(Alice)          datagram (prekey_id → Alice)       open with transit prekey
                                          ─────────────────────────▶         ↓ event.wire → event.decrypted
                                                                             ↓ validate.sync-blob → enumerate wanted slices
                                                                             ↓ slice events (0x03) for wanted parts
                                                                             ↓ seal_to transit prekey(Bob)

Delivery (Alice → Bob)
↓ slice (0x03, ciphertext under enc_key)  datagram (prekey_id → Bob)        open with transit prekey
                                          ─────────────────────────▶         ↓ event.wire → event.decrypted (slice has no event-layer enc)
                                                                             ↓ blob.manager: write slice_no → buffer
                                                                             ↓ when complete: verify root_hash → project.attachment → db.delta/insert(attachments)
```

## Event‑Layer Encryption (processor view)

- Key selection by `key_id`: every encrypted type carries `key_id` in its plaintext header. The dispatcher hydrates the referenced `key` event; decrypter opens with that key’s secret `G`. If open fails, mark invalid.
- AEAD: XChaCha20‑Poly1305 with a 24‑byte nonce; include an HMAC hint as needed.
- Signature: verify over plaintext header+payload (bytes 0–447 in plaintext form) after AEAD open.
- Storage: projection stores plaintext fields needed by queries; ciphertext can be retained for retransmit.

## Transit‑Layer Encryption

- Prekey‑sealed datagrams: each outgoing packet is sealed to a short‑lived transit prekey published by the recipient. The receiver opens with the transit prekey secret, attributes the packet by `prekey_id → (network_id, peer_remote_pk)`, and erases prekeys on TTL. The incoming event gains a `network_id` via this attribution.
- No sessions/handshakes: forward secrecy comes from frequent prekey rotation and erasure.
- Removal enforcement: refuse to open/process transit packets from removed peers/users.
- Outgoing pacing: `LOCAL‑ONLY‑outgoing` buffers sealed packets with `due_ms` for AIMD pacing; transports drain by due.

## Blobs

- Slices are AEAD‑authenticated under `enc_key`, not event‑layer encrypted; integrity via `root_hash` of all ciphertext slices.
- `slice_no` in clear → direct sparse writes to file/buffer; authenticated by `poly_tag` and final `root_hash`.
- Prioritized fetch: `sync‑blob(blob_id, window, bloom, limit)` for wanted ranges; track progress locally.
- Canonical IDs: `blob_id` is the `wire_id` of the root attachment update; slices reference that id and are processable even by peers unable to decrypt the parent message.

## Validation Policy (blocked/unblocked)

- Framework blocks on missing deps (including `key_id` for encrypted events), then unblocks dependents when referenced IDs validate, delivering hydrated context.
- Policy failures (e.g., signer not admin) can be rejected immediately; optionally keep blocked‑with‑reason if re‑tryable.

## DB Delta Shapes (examples)

- insert message: `db.delta/insert(messages, {event_id, channel_id, author_user_id, text, ts})`
- update rekeyed event: `db.delta/update(events, {id: original_event_id}, {ciphertext: new_ciphertext, key_id: new_key_id})`
- purge key: `db.delta/delete(keys, {id: old_key_id})`; purge prekey similarly
- insert attachment: `db.delta/insert(attachments, {message_id, blob_id, bytes, root_hash, nonce_prefix})`
- record slice (optional index): `db.delta/insert(slices, {blob_id, slice_no, path_offset, len})`

## Project Structure

- `protocols/quiet/event_types/`
  - `registry.py` (wire codes, sizes, per‑type `DEPS`) — under the protocol to keep `core/` protocol‑agnostic.
  - `schemas.py` (typed payloads)
- `protocols/quiet/sagas/`
  - `transit_inbound.py`, `event_parse.py`, `crypto_event_decrypt.py`
  - `validate_group.py`, `validate_channel.py`, `validate_message.py`, `validate_update.py`, `validate_rekey.py`, `validate_sync.py`
  - `project_group.py`, `project_channel.py`, `project_message.py`, `project_update.py`, `project_rekey.py`
  - `blob_manager.py`, `sync_scheduler.py`, `sync_responder.py`
  - `delta_apply.py` (framework‑provided single‑writer)
- `protocols/quiet/demo/` and tests mirroring sagas and end‑to‑end flows.

## Phased Implementation Plan

1) Core event pipeline
- Append‑only event store, `saga_applied`, dispatcher
- Delta applier saga and minimal `db.delta/*`
- event.parse, crypto.event.decrypt (AEAD scaffolding), sig.verify

2) Identity + Groups + Channels
- Implement validate/project for group, fixed‑group, grant, channel, channel‑update, delete‑channel
- Runner tests: creation → projection

3) Event‑layer encryption + Prekeys/Keys
- prekey|key validators; decrypt selection by TTL/tagId; blocked until keys arrive
- Happy‑path message send/recv with decryption + projection

4) Blobs
- add‑attachment update, slice ingest, reassembly, integrity check, projection; sync‑blob flow

5) Transit layer (prekey) + Sync
- LOCAL‑ONLY ingress/egress, transit prekey seal/open, sync scheduling/responding
- Removal enforcement at transit open

6) Rekey / Forward secrecy
- Planner: compute purge sets; emit rekey; validator: idempotent replacement; purge old keys/prekeys

7) Read receipts and polish
- seen events; unread queries; push stubs

## Testing Strategy

- Runner JSON tests per saga: given.events → when.events → then.emitted (including `db.delta/*`).
- Deterministic crypto (`CRYPTO_MODE=dummy`) for non‑transit tests; feed fixed prekeys/keys.
- Blob tests: small blobs with 3–4 slices; verify reassembly and root hash.
- Rekey tests: craft minimal keys/TTL windows; ensure rekey validation replaces ciphertext idempotently.

## Quick Reference Tables

Encryption‑related events

| Type | Code | Key fields | Dependencies |
|------|------|-----------|--------------|
| prekey | 0x19 | group_id, channel_id, prekey_pub, eol_ms | group/channel |
| key | 0x18 | peer_pk, count, created_ms, ttl_ms, tagId, prekey_id, sealed_key | prekey |
| rekey | 0x04 | original_event_id, new_key_id, new_ciphertext | original event, key |

Blob events

| Type | Code | Key fields | Dependencies |
|------|------|-----------|--------------|
| update:add‑attachment | 0x02/0x01 | blob_id, blob_bytes, nonce_prefix, enc_key, root_hash | message |
| slice | 0x03 | blob_id, slice_no, nonce24, ciphertext, poly_tag | blob root (via add‑attachment) |

Sync events

| Type | Code | Key fields | Dependencies |
|------|------|-----------|--------------|
| sync | 0x07 | window, bloom_bits | – |
| sync‑auth | 0x08 | window, bloom_bits, limit | – |
| sync‑lazy | 0x09 | cursor, bloom_bits, limit, channel_id | channel |
| sync‑blob | 0x0A | blob_id, window, bloom_bits, limit | blob root |

Channels & messaging (selected)

| Type | Code | Key fields | Dependencies |
|------|------|-----------|--------------|
| channel | 0x01 | group_id, channel_name, disappearing_time_ms | group/fixed‑group |
| channel‑update | 0x1F | channel_id, new_channel_name, new_disappearing_time_ms | channel |
| message | 0x00 | channel_id, text | channel |
| update | 0x02 | event_id, global_count, update_code, user_id, body | target event, user |
| delete‑message | 0x05 | message_id | message |
| delete‑channel | 0x06 | channel_id | channel |

— End —
