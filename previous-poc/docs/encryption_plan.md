# Encryption & Sync Plan

- Everything is transit wrapped/unwrapped (encrypted or sealed depending on key hint type)
- Wrap(id , plaintext) will seal if the id is a peer_secret or prekey_secret 
- Transit is agnostic as to encrypt or seal
-- Sync requests are sealed, naming a prekey_secret_id of the recipient peer in their deps, but not in the event props themselves, which resolve_deps resolves.
-- Sync responses are encrypted with the transit secret from the request
-- This is how we distinguish network membership
**NOTE: `prekey_secret_id` is a public hint carried by the recipient's `prekey` publication to allow senders to reference the correct prekey; senders never require access to the recipient's private prekey.**
- Everything *saved* is event encrypted/decrypted
-- Sync requests are not saved
-- All shared events are encrypted with the group they belong to, defaulting to main group.
-- All local events are encrypted with root_secret, which is not part of the ES and stored separately by core.
-- All events, shared and local, have hash(encrypted_event_blob) as their ID, and ES stored ED, blob.
**Status: Local-only secrets (`peer_secret`, `key_secret`, `prekey_secret`) are event‑encrypted under the process‑local `root_secret`. All saved events, including local‑only ones, use ciphertext‑derived IDs (blake2b(event_blob)). Functional identifiers (e.g., hash of a public key) may be carried in plaintext fields when needed, but do not determine `event_id`. `peer` events are event‑encrypted and include `key_secret_id` in deps.**
- Sealing/unsealing is done in particular events (sync_request, key) in their flows and reflectors.
- `key` events are always encrypted to a group (the main group for new member additions, the existing group for rotations) and unsealed by their reflector, which creates the respective `key_secret`. 
- Sync response creates a key_secret event, then includes its key_secret_id in deps under transit_secret, for transit_wrap (this is janky but more similar to the way unwrap works)
**NOTE: The current `sync_request` reflector does not create a `key_secret` on response; it reuses the per-request transit secret to send canonical events. Implementing this behavior requires adding that creation step and including the id of the created event as a dep.**
- To catch partitions, `sync-auth` reflector can check if a peer is missing a `key_secret_id` they should have if they are a member of a group, and share it with them directly covered by transit encryption.

## Key Primitives
- `peer_secret` (what we have been calling `identity`): local-only keypair; never gossiped.
- `peer`: public shared event corresponding to a `peer_secret` (public key + `peer_secret_id`). Used for identifying authorship, signing, and checking signatures.
- `key` (group key): symmetric key used for event-layer encryption; stored locally as `key_secret` and shared by sealing to recipients’ prekeys via `key` events.
**NOTE: In code, `events/key/reflector.py` still emits `secret_local` in places; standardize on `key_secret` everywhere.**
- `prekey`s: per-peer, per-scope short-lived keypairs. Public part is available to all network members (`prekey`); private part is local-only (`prekey_secret`).
- Invite/link keys: `invite-secret` keypair stored locally, public part is available to all network members in `invite`, keypair is re-derived from out-of-band secret by invitee for signing proof in `user` event (bootstrapping).

## Use Dependencies Wherever Possible
- Outgoing envelopes declare domain context as deps, not raw secrets:
  - Key hints for transit or event encryption cannot live inside (signed) event plaintext, or rekeying breaks sigs. Instead, key hints live as a prefix to ciphertext; once processed they also live in the envelope.   
  - Transit: `transit_secret:<transit_secret_id>` included in deps (comes from a local `transit_secret` event persisted from invite or created by a reflector)
  - Event: encrypted events include `key_secret_id` in deps which are then resolved by resolve_deps.
  - Signed: all to-be-signed outgoing events include `peer_secret_id` in envelope deps, which is then resolved by resolve deps, for signing. Signed events include `signed_by=peer_id` in their own props.
**NOTE: Current signing uses `peer_id` context and looks up the private key via `peers → peer_locals`; it does not require `peer_secret_id` in deps. If we adopt this requirement, the signature handler should read `peer_secret_id` from deps instead.**
- Deps are pipeline-local only and are never serialized on the wire; only the blob and its plaintext props are transmitted/stored.
- resolve_deps includes the event deps and encrypt/decrypt uses this.
- Crypto handler is pure: it reads only `resolved_deps`; no DB access, no special tables.
- Local-only deps like `key_secret_id` never “block” (failure to resolve is a local error, not a network wait).
**NOTE: Resolver currently blocks missing deps by inserting into `blocked_events`. If local-only `key_secret_id` should not “block”, we should treat missing keys as immediate local errors (drop) rather than queueing for unblock.**

## Ongoing Network Operation
- Prekey publishing: peers run job to periodically publish `prekey` (public) and keep `prekey_secret` (private, local-only).
- Group creation flow creates local `key_secret` event, then encrypts `group` event with it, and all `member` events. 
- Group member add flow creates `member` and `key` events, sealing `key_secret` to `prekey` (sealed to the public_key, using hint from `prekey_secret`) belonging to the member `user_id`.
- All events *including* `peer` name a `key_secret_id` and are encrypted by the encrypt_events handler, with resolve_deps providing keys.
- All events including `peer` are part of the main group if they are not part of another group
**NOTE: Implementation does not encrypt `peer` events and does not attach a `group_id` to them. Change flows/validators to include `key_secret_id` in deps, and group scoping.**
- `key` is encrypted to the main group too, so it is unsealed by its reflector, which creates the respective `key_secret`. now, event layer encryption applies to everything in the same way.
**NOTE: The existing key reflector still references `secret_local`; align it to `key_secret`.**
- `sync-auth` reflector can check if a peer is missing a `key_secret_id` they should have if they are a member of a group, and share it with them directly.   
- For outgoing events, resolve_deps resolves the key, and encrypt_event handler encrypts outgoing plaintext-only envelopes using this key.

## Transit Key Types & Hints

Transit uses two key modes and a single, normalized hint on the wire and in envelopes. Goals:
- Keep transit crypto pure and hint-driven (no DB reads in crypto handlers).
- Attribute network early for gating without trusting payload plaintext.
- Support both initial KEM requests (sealed to a prekey) and subsequent DEM responses (encrypted under a symmetric transit secret) in the same framing model.

### Types
- prekey (KEM): short-lived per-address public key published as `prekey` (public) and stored locally as `prekey_secret` (private). Used to seal sync requests and first-contact join messages.
- transit_secret (DEM): short-lived symmetric key used to encrypt responses and ongoing traffic. Stored locally as a `transit_secret` event (local-only) with TTL metadata. Its `transit_secret_id` is the event_id assigned by encrypt_event (root-encrypted ciphertext id).

These are distinct for security: prekeys are public (validate reachability, enable one-way confidentiality), while transit secrets are private, symmetric, short-lived tokens that gate DEM traffic.

### Wire framing and receive_from_network
- Wire (DEM or KEM): hint_id (event_id of transit_secret OR prekey_secret), ciphertext

- receive_from_network behavior:
  - Parses header and emits envelopes with hint_id, ciphertext

- resolve_deps resolves the appropriate dep into `resolved_deps` from ids:
  - transit: resolved_deps['transit_secret:<id>'] = { transit_secret, network_id }
  - sealed: resolved_deps['prekey_secret:<id>'] = { prekey_private, prekey_public, network_id }

### Network Gate (Consistency Check)
- Purpose: Enforce that the network attributed via the resolved transit key matches any network stated in the decrypted plaintext; prevent cross-network replay/misattribution.
- Placement: Runs after `resolve_deps` and after decrypt/open (i.e., when `event_plaintext` is present), before per-type validators.
- Inputs:
  - From deps: `network_id` on either `resolved_deps['transit_secret:<id>']` (DEM) or `resolved_deps['prekey_secret:<id>']` (KEM).
  - From plaintext: `event_plaintext.network_id` (optional).
- Behavior:
  - If `envelope.network_id` is unset, set it from the dep-derived `network_id`.
  - If `event_plaintext.network_id` is present, require it equals `envelope.network_id`.
  - On mismatch, drop the envelope (do not emit content). This prevents any downstream processing of misattributed events.
- Notes:
  - No DB tables or indexing; this is a pure pass-through check that uses only the resolved deps already in the envelope.
  - Crypto handlers remain pure; attribution derives exclusively from local, validated secret material.

### Sender behavior and selection
- Joiner bootstrap:
  - Use the invite-provided `prekey_public`/`prekey_secret_id` to seal the initial sync_request (KEM) to the inviter; the wire hint is `prekey_secret_id` (recipient’s prekey id).
  - Persist a `transit_secret` locally (transit_secret_id = event_id(transit_secret)) with TTL and `network_id` for DEM responses and ongoing traffic.
  - Persist a `key_secret` for event-layer bootstrap per invite (separate from transit) if required by flows.
  - Publish the joiner’s own `prekey` (public) and store `prekey_secret` (private) with TTL so the joiner can receive sealed packets.

- Outgoing KEM (requests and first-contact messages):
  - Use the recipient’s `prekey` (KEM) with SealedBox for sync_request and initial join messages; the wire hint is `prekey_secret_id`.
  - open_sealed_event uses `resolved_deps['prekey_secret:<id>']` to unseal.

- Outgoing DEM (responses and ongoing traffic):
  - Use `transit_secret` (DEM) to encrypt a response that carries `event_blob_hex` in the transit plaintext.
  - receive_from_network on the receiver emits `transit_secret_id` and `transit_ciphertext`; transit_unwrap uses `resolved_deps['transit_secret:<id>']`.

### Event content and naming
- transit_secret (local-only event type):
  - event_plaintext fields:
    - type: 'transit_secret'
    - unsealed_secret: hex
    - network_id: string
    - dest_ip, dest_port: optional hints for jobs during bootstrap
    - expires_at_ms: optional TTL (short; minutes)
    - created_at, created_by

- prekey/prekey_secret:
  - prekey (public, network event) includes: prekey_secret_id, peer_id, network_id, prekey_public, created_at, ttl.
  - prekey_secret (local-only) includes: prekey_private, prekey_public, network_id, created_at, ttl.

### TTL and rotation
- prekey_secret: very short TTL (seconds to minutes); multiple may overlap; receivers select the newest valid one by id match.
- transit_secret: short TTL (minutes); reflector includes fresh transit_secret in responses and rotates periodically.

### Naming summary
- Hint kinds: 'prekey_secret' (KEM), 'transit_secret' (DEM).
- Envelope helper fields (for convenience): `prekey_secret_id`, `transit_secret_id`.
- Resolved deps: `prekey_secret:<id>`, `transit_secret:<id>`.

### Minimal flow examples
- Sync request (joiner → inviter):
  - Sender seals plaintext with recipient prekey; wire header uses mode=0x02 and 16-byte `prekey_secret_id`.
  - Receiver attributes network from `resolved_deps['prekey_secret:<id>']`, unseals via `prekey_secret:<id>`, validates, reflects.
  - Reflector chooses/creates `transit_secret`, includes id in response deps, and encrypts DEM responses under it.

- Sync response and subsequent traffic (inviter → joiner):
  - Sender encrypts DEM using transit_secret; wire header uses mode=0x01 and 16-byte `transit_secret_id` (event_id of transit_secret).
  - Receiver attributes network from `resolved_deps['transit_secret:<id>']`, decrypts transit with `transit_secret:<id>`, then decrypts event layer.

This approach keeps the two modes explicit, allows network attribution before crypto, and makes the crypto handlers pure and hint-driven while preserving short-lived semantics and clear boundaries.

## Bootstrap Invitation (invite/link)
- Core on first app start creates `root_secret` and provides it and a root_secret_id (hash) to pipeline
- Network creator encrypts all local `*_secret` events with `root_secret`. event_decrypt handler checks root_secret. (Think this through more.) 
**Status: `root_secret` is implemented and used to event-encrypt local-only secrets. IDs for these remain their functional IDs (see above) to keep resolver references stable.**
- Inviter creates: 
 - a secret, derives a keypair from it, and creates an invite event (encrypted to main group) and invite link.
 - `key` events sealing all keys for the main group to this new public key.
 - a fresh `key_secret` the invitee can use for event-layer encryption until they get other keys, with the secret in the invite event.
- All new main group keys (if rotated) are also sealed to all existing invite events in the same way.
- Invite event:
 - Includes pk of keypair so others can verify invite proof from invitee
 - Includes `network_id`
- The transit `key_secret` that the invitee can use to do initial transit encryption DEM (**not kem to the invite pk, because that will only be visible to the inviter**).
**NOTE: Naming: this event prop should be named transit_secret but the event it refers to is still a `key_secret`.**
- Invite link includes:
 - `network_id` and a current `address` for reachability.
 - Initial invite secret the keypair was derived from, for re-deriving the keypair, signing proof, and unsealing `key` events.
 - `key_secret` key material to allow the joiner to encrypt event layer.
 - an `address` for joining (does not have to be inviter's address) 
 - `prekey` pk and `prekey_secret_id` corresponding to the address, for sealing `sync_request`
  
## Bootstrap Joining (invite/link)
- Joiner creates `peer_secret` and `peer` locally.
- Using the invite payload:
  - Transit (requests): encrypt to the invite transit secret until `prekey`s sync.
  - Event-layer: encrypt initial `user`, `peer`, `address`, and `prekey` publications using the provided event secret until normal keys are available.
**NOTE:** `peer` events are event‑encrypted (not plaintext) and include a `key_secret_id` dependency for encryption.
- Joiner sends the above envelopes; once validated, the inviter’s `send_sync` includes the joiner, and the joiner’s `send_sync` includes other peers after validation.

## Rekeying & Forward Secrecy (FS)
- Goals: protect in-flight messages via transit PFS/PCS and protect deleted/expired data from later recovery by re-encrypting surviving plaintext under fresh keys.
- Triggers: when events expire or are deleted, mark their event-keys and the prekeys those keys were encapsulated to as must-purge.
- Rekey sweep:
  - Choose a clean key whose ttl is minimally greater than each target event’s ttl.
  - Create a `rekey` event per target that deterministically re-encrypts the original plaintext under the new key using a deterministic nonce such as `HASH(original_event_id || new_key_id)`.
  - A rekey is valid if it decrypts to the same plaintext as the original and uses the deterministic nonce correctly. If multiple rekeys target the same event, keep the one using the key with the closest-but-greater ttl; discard others.
**NOTE: Deterministic nonces must guarantee uniqueness per key; prefer `nonce = HKDF(label='rekey-nonce', key=new_key, info=old_event_id || new_key_id)[:nonce_len]`.**
**NOTE: Rekey events and validation logic are not implemented yet; this will require a new event type (`rekey`) and related projector/purge mechanics.**
- Single-ID policy: we will not introduce dual ids (no separate `content_id` vs `blob_id`). We cannot dedupe rekey events we cannot decrypt; once decrypted, dedupe by content rules and stable signatures. Historical ids beyond the immediately superseded blob need not be preserved.
- Purge sweeps (independent, periodic):
  - Purge original events once corresponding validated `rekey` events exist.
  - Purge must-purge keys and prekeys that have no remaining not-deleted events.
  - Independence means no complex transactions; if interrupted, later sweeps complete the work.
- Performance notes: rekeying is faster if event-keys aren’t reused across channels and if prekeys aren’t reused by multiple key events.
 - Scope boundary: we do not attempt FS for not-yet-purged stored history (Slack-like sharing). Transit PFS/PCS is provided by sealing packets to prekeys (fallback: peer/invite pk); storage FS for deleted/expired data is achieved via rekey + purge cycles.

## Sync
- Sync requests:
 - sent to a specific peer address and include peer_id and address_id.
 - sealed to a prekey. (**question: is it worth complicating transit layer encryption by introducing sealed data at that layer? or should we encrypt to a recipient-specific symmetric key (recipient specific so that it doesn't leak more about group membership than the hint from sealing would-- the thing we don't want is for group key prefixes to identity everyone in a group**)
 - include fresh per-request symmetric key ("key_secret") and its `key_secret_id` (created in request job flow) plus window/bloom or lazy cursor.
**NOTE: Naming mismatch: code calls the per-request transit key `transit_secret`. Note that we can call the prop transit_secret but the key we are referring to is just a normal key.**
 - sealed to the recipient's `prekey`, unsealed by `sync_request` reflector 
- Outgoing response envelopes (symmetric transit using request’s key):
  - Consist of canonical events.
  - transit_wrap encrypts each response with the per-request `transit_secret` (included in envelope deps by reflector)
- Received responses:
  - transit_unwrap extracts the event-layer blob using the transit key
  - if cannot decrypt, drop
  - once decrypted, normal pipeline and validation, dedupes by event id (encrypted event hash).
**NOTE: transit_unwrap must remain pure; network attribution is enforced by network_gate consistency checks (no indexing required).**
**NOTE: With the single-id policy, response-side dedupe by event id only dedupes identical ciphertext. Rekeyed events received before decryption cannot be deduped until their plaintext is available.**

### Sync mechanics (concise)
- Trigger: a periodic job (e.g., "send_sync") emits requests to active peers for a network; variants like "send_sync_auth" may prioritize auth-related state.
- Response path: a reflector processes incoming requests, extracts the response `transit_secret` from the decrypted request, and emits responses encrypted symmetrically with that secret (no public-key sealing for responses).
- Storage: do not store sync-request events; store new response events after dedupe by event_id.
- Correlation of response to request, and to network, is by the transit secret used; responses include `transit_secret_id` prefix (from the request).
- Queries: implement minimal queries such as get_active_peers, get_active_prekey_for_peer, get_events_for_sync (windowed and/or bloom-filtered).
- Limits: rate-limit request cadence (e.g., ~3s) and cap response size (e.g., ≤ N events) to avoid flooding (use `due_ms` in outgoing envelopes to cadence).
- Validation: reject transit from removed peers; honor removal rules when choosing recipients for new keys/events.

## Addendum: Clarifications and TBD Decisions
- Deps are local-only: `deps` are pipeline-local and never serialized on the wire. Only the encrypted event blob and its plaintext props are transmitted/stored.
- Prekey hint is public: Using `prekey_secret_id` as a public hint in `prekey` events is acceptable. Senders may reference `prekey_secret:<id>` in deps to trigger sealing; the resolver maps that id to the recipient's public `prekey`. The id must be an opaque, random identifier (not derived from secret bytes in a reversible way--hash is okay), and senders never need access to the recipient's private prekey.
- Key hints outside the blob: Hints such as `key_secret_id` (event-layer) and `transit_secret_id` (transit) live outside the encrypted blob (and in local deps) for routing/unseal. Do not include these hints in the signed plaintext.
- Signing scope: Sign canonical plaintext only. This keeps the signature stable across rekeys because hints/crypto metadata are excluded from the signed content.
- Rekey and IDs (TBD, single-id policy): We will keep a single event id to minimize deletion mistakes and avoid dual ids (e.g., no separate `content_id` vs `blob_id`). As a consequence, we cannot dedupe rekey events we cannot decrypt; once we can decrypt, we dedupe based on content rules and signature equivalence. The precise rekey event shape and validation remain TBD under this single-id constraint.
- Transit/network attribution: Do not trust `network_id` carried in transit payloads. After resolve_deps + transit_unwrap, network_gate compares network ids from deps vs plaintext and drops on mismatch.
- Resolver behavior: Classify deps (e.g., network vs local-only). Missing local-only deps (like `key_secret_id` on this node) should not "block"; treat them as immediate local errors rather than inserting into a network unblock queue.
- Naming consistency: Standardize on `key_secret` (replace remaining `secret_local` usages) and use `transit_secret` as the property name for the per-request symmetric transit key that is, in storage, a `key_secret`.
- Bootstrap considerations: If `peer` events become encrypted to hide membership, ensure the invite/join flow provides sufficient keys/hints to validate and decrypt bootstrap events. If `peer` remains public, avoid including group-binding hints in its plaintext props.
- Deterministic nonce (optional): If deterministic nonces are used for rekey, define them to be unique per `(new_key_id, old_event_id)` (e.g., via HKDF) to prevent nonce reuse. Random nonces remain acceptable; validation compares decrypted plaintext and signatures.

## Implementation Steps (Roadmap)

### Phase 1 — Low-Hanging Fruit (Correctness, Consistency, Safety)
- Standardize names: Replace all `secret_local` references with `key_secret`. Use `transit_secret` for the per-request symmetric transit key property. Align event/handler names, and update `openapi.yaml` and protocol_types / core_types where applicable.
- Update README.md to be consistent with latest naming and design
- Root secret for local-only: Implement `root_secret` and encrypt local-only `*_secret` events with it so their event ids are derived from ciphertext (single-id policy preserved). 
- Do not worry about migration or legacy data.
- Move key hints out of plaintext: Ensure `key_secret_id` (event-layer) and `transit_secret_id` (transit) are carried as ciphertext prefixes and in local deps, not inside signed plaintext. Update encrypt/decrypt handlers accordingly.
- Signing scope: Sign canonical plaintext only (exclude hints/prefixes). Update validators to check signatures over decrypted canonical plaintext.
- Deps behavior: Confirm `deps` never serialize; add tests asserting outbox payloads contain no `deps`. Classify deps; make local-only deps non-blocking (fail-fast locally instead of enqueueing to unblock).
- (Deprecated) Any indexed mapping for attribution is unnecessary; attribution derives from local secrets and is enforced by network_gate.
- Prekey hints: Ensure `prekey` publications carry an opaque `prekey_secret_id` as a public hint. Senders reference `prekey_secret:<id>` in deps; resolver looks up the corresponding public `prekey` for sealing.
- Peer event posture: Short-term, keep `peer` public and ensure no group-binding hints are present in plaintext. If encrypting `peer` is prioritized, add main-group scoping and update validators/flows.
- Rate limits and sizes: Enforce basic request cadence (e.g., ≥3s) and response caps by honoring `due_ms` and size limits in handlers to avoid reflector amplification.

### Phase 2 — Bootstrap & API-Only Scenarios
- Invite/link flow hardening: we want new joiners to be able to send sealed data to the address they are given. Note that this is not necessarily the inviter's address; it can be any address the inviter client chooses. Note also that the sealing key must be different than the proof of invite key, because we don't want to give this address the ability to use the invite link itself by e.g. generating an invite proof. So the invite event includes a public key for verifying the proof (the private key can be forgotten) and the invite link contains both the secret to derive the keypair for the proof *and also* a public key (prekey public) and the prekey_private_id for the peer address in the invite link. **note: this implies that transit_unwrap prefix hints may resolve to prekey_secret_id and require unsealing (KEM)**
Use a fresh `key_secret` and `key` (with ttl equal to the invite ttl) wrapping it to all peers, the `key_secret_id` (as transit_key_id) in the invite event and the secret itself in the invite link for initial transit, until prekeys sync.
- Initial publications under limited keys: Allow joiner to encrypt `user`, `peer` (if encrypted), `address`, and `prekey` with the provided `key_secret` (or a `key` sealed to the invite pk) until normal group keys are available.
- Do not store sync_request: Ensure requests are transient; only store validated response events after dedupe by event id (identical ciphertext) or post-decrypt content rules when available.
- Scenario tests focus: Bring these closer to passing using only API calls (single DB):
  - `tests/scenarios/test_kem_dem_roundtrip.py`
  - `tests/scenarios/test_simulator_transit_roundtrip.py`
  - `tests/scenarios/test_multi_identity_chat_sim.py`
  - `tests/scenarios/test_overlapping_networks_sim.py`
  Update flows/handlers so that: requests seal to recipient `prekey`; responses use the request’s `transit_secret`; decrypt path attributes network via the mapping; validators enforce signatures on canonical plaintext.

### Phase 3 — Ongoing Operation
- Prekey lifecycle: Periodic publishing/rotation; queries for `get_active_prekey_for_peer`; avoid reusing prekeys across unrelated key events.
- Send/receive sync: Implement `get_events_for_sync` (windowed/bloom-filtered), respect removal rules, enforce limits, and reject transit from removed peers.
- Rekey groundwork under single-id policy: Accept rekeyed blobs when decryption is possible; dedupe after decrypt using content/signature rules. Optionally keep a short-lived redirect from old→new ids to bridge in-flight references; purge old blobs by TTL once validated.
- Purge sweeps: Periodic, independent GC for purging originals after validated rekeys and removing must-purge keys/prekeys with no remaining live events.

Notes: Option A embedded transit-key indexing inside `transit_unwrap` (fast path) is deprecated; we keep crypto handlers pure and perform consistency checks in network_gate.
- A rekey event made by Alice should be the same as a rekey event made by Bob, if our convergence strategy works and they choose the same target key.
