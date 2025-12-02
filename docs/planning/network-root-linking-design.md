# Network Root Linking Design (No‑Stubs, Uniform Invites)

Meta Goals

- Connection should be testable without full DAG or syncing (just peers, transit_prekeys, proofs of invite or peer)
- Syncing should be testable without connection (just transit_key and sync_requests and arbitrary shareable blobs)
- Invite and proof events (invite + user/peer_shared signed_by=invite_id) should be testable in isolation
- User invite and peer linking should be testable without connection or syncing (1 player)
- Groups should be testable without syncing or encryption or invitation (just users, links, group members)
- Encryption should be testable without invitation
- Invitation can optionally grant access to encryption secrets via a prekey in the invite data

Goals for linking

- Uniform: the invite process to add a peer is identical for the first peer and all later peers.
- Uniform: the invite process to add a user is identical for the first user and all later users.
- Simple DAGs: two steps to create a user, two steps to link a peer, no special cases.
- All DAG relationships can be expressed and tested in "single-player" scenario, independent of connection or syncing, so single-player tests passing is sufficient proof that multi-player scenario tests will pass.

Network and Admin (Implicit Policy)

- Network authority usage is minimal and short‑lived:
  - The network (creator authority) signs `invite(mode=user)` and a single `admin` for the first user, then the network signing key is discarded/offline.
- After the user's first peer is added and the user is `admin`, they can create everything needed (channels, additional invites, etc.) as any admin would; no additional bootstrap cases.
- Admin gating rule: admin‑only ops must be signed by a peer linked to a user who is an admin (`signed_by`), no separate policy document.

Identity Layers

- User: content‑addressed `user_id = id(invite(mode=user))`, has `user_pubkey` (used to issue the first peer invite, then discarded/off‑device).
- Peer identity: `peer_shared_id = id(peer_shared)` publishes the peer public key used for subsequent peer‑signed events (post‑link).
- Most events include `signed_by = peer_shared_id` and must verify with the peer public key from `peer_shared` (peer ops are valid only after link).
- Local‑only peer: a `peer` event exists locally to guard `recorded_by`; it is never shared and has no DAG dependency with `peer_shared`. (It does not need one for event store syncing/playback/convergence to work because only local `recorded_by` events and the creation of new shared events require `peer` to have been projected.)

- Peer vs Local Peer

- `peer_shared` (public): content‑addressed peer identity used to verify peer‑signed events after link. It carries an invite-key signature over the entire event body (excluding the signature field); it is accepted when the referenced invite exists and the signature verifies.
- `peer` (local‑only): never shared; holds the peer’s private key material and guards `recorded_by` (`recorded_by` refs and depends on `peer`) for local persistence and partitioning. There is no shared DAG edge between `peer` and `peer_shared`.
- Local assertion (optional): when publishing your own `peer_shared`, assert locally that `peer_shared.pubkey == peer.pubkey` to catch misconfigurations; remote projectors do not enforce this.
- Ordering: create `peer` locally (to enable `recorded_by`), then publish `peer_shared` once you have the invite link/material. Remote projectors will block `peer_shared` on the invite if needed; no separate acceptance event is required for peers. `peer` must be created before any other event can be recorded and projected.

Events (content‑addressed)

- `network`: network creation; `id(network)` is `network_id`; establishes the network public key (root authority).
- `peer_shared`: peer public key record; deterministically identifies the peer post‑join.
  - Fields:
    - `invite_id`: id of the corresponding `invite(mode=peer)`.
    - `pubkey`: the peer public key.
  - Signing: `signed_by = invite_id` (signature over canonical peer_shared body, excluding the signature field).
  - Dependencies: list `invite(invite_id)` as a DAG dependency. No separate peer acceptance event is used; the signature with the invite key replaces it.
  - Verification (projector): load `invite` by `invite_id`; verify the event signature with `invite.invite_pubkey` over the canonicalized peer_shared body. If valid, proceed to link.
  - Security notes:
    - The signature cannot be forged without `invite_private_key`. A malicious party lacking that key cannot produce a valid signature.
    - The signature is bound to the entire peer_shared body (including `invite_id`, `pubkey`, and any other fields); it cannot be replayed to create a different `peer_shared_id`.
    - Replaying an identical `peer_shared` byte‑for‑byte is harmless: content addressing makes it the same event id; projection uses INSERT OR IGNORE.
- `invite`: signed_by. One invite type for both user creation and peer linking.
  - kind=`user` (create a user slot):
    - Fields:
      - `network_id`: the network this user belongs to.
      - `invite_pubkey` (projects to `user_pubkey`): public key the joiner uses to accept the invite; on projection we persist this as the user’s `user_pubkey`, which is then used to verify `signed_by = user_id` operations (e.g., first peer invite).
      - `created_at`: event creation time used for auth/time checks.
      - `admin_grant` (ongoing only): id of a prior `admin` event establishing the signer’s admin status; explicit DAG anchor for authorization.
    - signed_by: bootstrap → `network_id` (verified with the network public key); ongoing → a `peer_shared_id` of a user who is currently an admin (verified with that peer’s public key).
  - kind=`peer` (link a new peer to an existing user):
    - Fields:
      - `user_id`: the user this peer will attach to.
      - `invite_prekey_id` (optional): id of a prekey the inviter published to encrypt initial bootstrap secrets (group keys, etc.) to the joiner. The invite link contains the corresponding private material; the event only references the id.
      - `invite_pubkey`: public half of a one‑time "invite keypair" minted by the inviter for this invite. The invite link contains the matching `invite_private_key` so only the intended joiner can accept. 
    - Semantics:
      - Identity proof: the joiner publishes `peer_shared` signed by the corresponding invite key (`signed_by = invite_id`).
      - Identity bind: projector verifies the signature with `invite_pubkey` (no separate equality check is required since the signature covers the full body, including `pubkey`).
    - Authorization: signed_by=`user_id` (first peer) or by any currently linked `peer_shared_id` of that user (later peers).
- `user`: user identity record; deterministically establishes the user created by a user invite.
  - Fields:
    - `invite_id`: id of the corresponding `invite(mode=user)`.
    - `user_pubkey`: the user's OWN fresh keypair (NOT derived from invite_pubkey). This separation is critical: a single invite can create many users, each with unique identity and keypair.
    - `name`: display name for the user.
    - `created_at`: timestamp.
    - `network_id` (optional): extracted from invite, included if present.
  - Signing: `signed_by = invite_id` (signature over canonical user body, excluding the signature field).
  - Dependencies: list `invite(invite_id)` as a DAG dependency.
  - Verification (projector): load `inv_u = invites_user[invite_id]`; verify the event signature with `inv_u.invite_pubkey` over the canonicalized user body.
  - Projection: INSERT OR IGNORE `users(user_id=id(user_event), user_pubkey=user_event.user_pubkey, ...)` and mark valid.
  - Note: `user_id = id(user_event)` (content hash), NOT `invite_id`. One invite can create multiple users.
- `admin`: signed_by. Grant of admin to `user_id` for `network_id`.
  - Fields: `user_id` (subject), `created_at`.
  - Bootstrap: signed_by=`network_id` to grant the first admin (before retiring the network signing key).
  - Ongoing: signed_by a linked peer_shared of a user who is an admin (admin‑only ops must be admin‑peer‑signed).
  - Field (ongoing): `admin_grant` — id of a prior `admin` event establishing the signer’s admin status; explicit DAG anchor for authorization.
  - Dependencies and gating:
    - Always depends on the subject `user` (the user who is being granted admin).
    - Bootstrap additionally depends on `network` (present to verify network signature).
    - Ongoing additionally depends on (expressed via DAG refs):
      1) the signer’s `peer_shared` (ensures the signer peer is linked);
      2) `admin_grant` (ensures prior admin evidence is present to make the signer an admin; ordering is enforced by the explicit dependency, not wall‑clock time).

Uniform DAGs

- Create User (no peer yet):
  - `invite(mode=user)` (signed_by network/admin) → `user (signed_by=invite_id)` → `users(user_id = id(user_event))`
- Link Peer (first and later identical):
  - `invite(mode=peer)` (signed_by authorized signer) → `peer_shared (signed_by=invite_id)` (signature by the peer invite key over the entire peer_shared body verifies) → `linked_peers(user_id, PX)`

- Signature Model (signed_by)

- Rule: all events except `network` include `signed_by` + `sig` and verify by resolving the signer’s public key by type:
  - `signed_by = network_id`: verify with `network_pubkey`.
  - `signed_by = user_id`: verify with `user_pubkey` from `users`/`invites_user`.
  - `signed_by = peer_shared_id`: verify with the peer public key from `peer_shared` (only after link).
  - `signed_by = invite_id`: verify with the invite public key (`invites_user.invite_pubkey` for `user`; `invites_peer.invite_pubkey` for `peer_shared`) over the canonical body (excluding the signature field).
- Authorization checks (in addition to signature):
  - `invite(mode=user)`: bootstrap signed_by network; ongoing signed_by a peer of an existing admin user.
  - `invite(mode=peer)`: for first peer, signed_by user; for later peers, signed_by a currently linked peer of that user.
  - `admin`: bootstrap signed_by network; ongoing signed_by a linked peer_shared of a user who is admin (anchored via `admin_grant`).
  - Validation deferral: evaluation uses only declared DAG dependencies below; if any dependency is missing, the event is blocked and retried when it arrives. No special local flags are required.

Authorization Dependencies Triplet (admin-only ops)

For any admin-only operation (e.g., invite(kind=user) ongoing, admin ongoing), authorization requires a matching triplet of dependencies so we can unambiguously match user/admin/peer:

- Subject user: the `user(subject)` that the operation concerns.
- Signer peer: a linked `peer_shared(signer)` that maps via `linked_peers` to a signer user.
- Admin grant: `admin_grant` referencing a prior `admin` event for the signer user in the same network.

The projector enforces matching constraints when all dependencies are present:
- linked_peers(peer_shared_id) → signer_user_id.
- admin(admin_grant).user_id == signer_user_id.
- admin(admin_grant).network_id == this.network_id.

If any dependency is missing, the event remains blocked and is re-evaluated when the missing dependency arrives (normal DAG blocking; no local flags).

Invite and Acceptance Flows (User and Peer)

Required Dependencies (per event and why)

- `network`:
  - Depends on: (none). Root of DAG.
  - Why: Establishes `network_id` and `network_pubkey`.

- `invite(mode=user)`:
  - Bootstrap depends on: `network`.
  - Ongoing depends on: `peer_shared(signer)`, and `admin_grant` referencing prior `admin(signer_user)` that establishes the signer’s admin status (ordering by dependency).
  - Why: Verify network/admin signatures for authorization; ensure signer peer is linked and has admin power at issuance time; explicit anchor via `admin_grant`.

- `user`:
  - Depends on: `invite(mode=user)`.
  - Why: Verify signature with the `user_pubkey` named in the invite (since `signed_by = invite_id`); establishes `users(user_id)`.

- `invite(mode=peer)`:
  - First depends on: `user` (subject user exists).
  - Ongoing depends on: `user` and `peer_shared(signer)` (signer linked).
  - Why: Authorization evaluates against the user’s state; first invite may be signed by `user`; later by a linked peer of that user (no admin required).

- `peer_shared`:
  - Depends on: `invite(mode=peer)` (by `invite_id`).
  - Why: Verify signature with `invite.invite_pubkey` (since `signed_by = invite_id`); link the peer to `invite.user_id`.
  - Note: Any additional local references (e.g., creator’s local peer) do not add DAG edges and are not considered dependencies for admissibility.

- `admin`:
  - Bootstrap depends on: `network`, `user(subject)`.
  - Ongoing depends on: `user(subject)`, `peer_shared(signer)`, and `admin_grant` referencing prior `admin(signer_user)` that establishes the signer’s admin status (ordering by dependency).
  - Why: Verify signature (network or peer), ensure signer peer is linked, and ensure signer’s user has admin rights at the time of issuance; if any are missing, normal DAG blocking applies until present; explicit anchor via `admin_grant`.

Event Summary (Fields • Signed-by • Depends-on)

```
event: network
  fields: network_pubkey, created_at
  signed_by: —
  depends_on: —

event: invite(user)
  fields: network_id, invite_id, user_pubkey, created_at, admin_grant?
  signed_by: network (bootstrap) | admin_peer (ongoing)
  depends_on: network (bootstrap) | peer_shared(signer), admin_grant (ongoing)

event: user
  fields: invite_id, user_pubkey (fresh keypair), name, created_at, network_id?
  signed_by: invite_id (verify with invite(user).invite_pubkey)
  depends_on: invite(user)

event: invite(peer)
  fields: user_id, invite_prekey_id?, invite_pubkey, created_at
  signed_by: user (first) | linked peer of user (ongoing)
  depends_on: user (first) | user, peer_shared(signer) (ongoing)

event: peer_shared
  fields: invite_id, pubkey
  signed_by: invite_id (verify with invite(peer).invite_pubkey)
  depends_on: invite(peer)

event: admin
  fields: user_id (subject), created_at, admin_grant? (ongoing)
  signed_by: network (bootstrap) | admin_peer (ongoing)
  depends_on: network, user(subject) (bootstrap) | user(subject), peer_shared(signer), admin_grant (ongoing)
```


- Network creation and first user:
  - `network`: establishes `network_id` and `network_pubkey`.
  - `invite(mode=user)`: payload `network_id`, `invite_id`, `user_pubkey`, `created_at`; signed_by=`network_id` (bootstrap). Projection persists `invites_user(...)`.
- `user` (establish identity): fields include `invite_id`, `user_pubkey` (fresh keypair), `name`, `created_at`, and optional `network_id`. Event is signed by the invite key (`signed_by = invite_id`, signature over canonical user body). Projector depends on `invite(mode=user)`, verifies the signature with `invite_pubkey` from `invites_user`, and inserts `users(user_id=id(user_event), user_pubkey=user_event.user_pubkey)`.
  - `admin` (bootstrap): signed_by=`network_id` to grant admin to the newly created `user_id` before retiring the network signing key.
  - Hygiene: after issuing the first peer invite, keep `user_private_key` off‑device or discard.

- Inviting subsequent users (same flow, different signer):
  - `invite(mode=user)`: signed_by a `peer_shared_id` belonging to a current admin user; otherwise identical fields/behavior.
  - `user`: same verification and projection; creates `users(user_id=id(user_event), user_pubkey=user_event.user_pubkey)`.
  - `admin` (optional): grant admin via a peer of an existing admin user as policy requires.

- Linking peers to a user (first and later are identical):
  - `invite(mode=peer)`: payload `user_id`, `invite_prekey_id` (optional), `invite_pubkey`, `created_at`; signed_by=`user_id` for the first peer, or any currently linked `peer_shared_id` of that user thereafter. Projection persists `invites_peer(...)`.
- `peer_shared` (links the peer): fields include `invite_id`, `pubkey`. Event is signed by the invite key (`signed_by = invite_id`, signature over canonical peer_shared body). Projector depends on `invite(invite_id)`, verifies the signature with `invite.invite_pubkey`, and then inserts `peers_shared(...)` and `linked_peers(user_id, peer_shared_id)`.
  - Local sequencing (creator): create local `peer` first (to sign/gate `recorded_by`), then publish `peer_shared` once you have the invite material.

Targets (Post‑Projection)

- What targets are: Targets are local, deterministic derived actions (not wire events). They do not add DAG edges or create new shared events. Prefer expressing validation order via explicit DAG dependencies. Use targets for local side effects (e.g., cleanup, rekey) that do not affect admissibility.
- Run‑if‑present / Run‑on‑arrival semantics:
  - If the named trigger is already projected locally, run the target handler immediately.
  - If the named trigger is not present yet, persist a local registration. The handler runs once, automatically, as soon as the trigger projects.
- When targets run:
  - On‑arrival (pre‑validation): by default, ephemeral only. Do not perform durable writes for events that may later project; restrict to safe, transient actions (telemetry, in‑memory caches, UI hints/spinners, best‑effort pings) that can be ignored if the event is later rejected.
    - Exception — deletions: for events that are never intended to project (e.g., deletion suppression where the projector will reject the target event), durable cleanup on‑arrival is allowed to minimize exposure. This exception must remain replay‑deterministic and idempotent.
  - Post‑projection (preferred): durable side effects that run only after the event is validated and projected; safe to persist and replay (e.g., insert linkage rows, update indexes, enqueue idempotent background jobs).
- Implementation (no new shared primitive):
  - Event fields (optional): `target_id` (at most one per event). The source event type determines the handler logic; the target is the trigger event id being awaited.
  - Maintain a local `targets(target_id, targeted_by, status, executed_at)`.
  - On projecting any event, dispatch post‑projection handlers for matching targets (preferably in the same transaction for atomicity). After a handler runs, either mark the row `executed` (for audit/replay) or delete the registration immediately (common case; avoids GC like we do for the blocked‑queue). Both strategies are supported; choose per product needs.
  - Handlers also run synchronously if the trigger is present at registration time.
- Interaction with blocking/toposort: Targets don’t add edges; blocking/toposort orders shared projections, and target execution follows that order.
- No network target is required: admissibility and ordering are expressed via DAG dependencies only.
- Other examples (post‑projection):
  - Peer removal: on projecting a peer‑removal event, schedule/perform group rekey and bump counters locally; future peer‑signed ops must fail auth (“signer peer is currently linked?” check).
  - Channel or user deletion: immediately suppress UI/processing on arrival (non‑durable), and after projection mark rows deleted and invalidate prior projections deterministically.

- Bulk targets (class‑wide, persistent):
  - Purpose: apply a local handler to every future event that references a given id (e.g., all events for `channel_id`) until deactivated. Complements single‑trigger `targets`.
  - Registration: projector code (or a post‑projection handler) inserts a persistent registration when needed (e.g., on `channel_deleted`).
  - Schema (local‑only): `bulk_targets(bulk_target_id BLOB, targeted_by BLOB, status TEXT, executed_at INTEGER, PRIMARY KEY(bulk_target_id, targeted_by))`
    - `status ∈ {active, inactive}`; set to `inactive` when the bulk target is retired.
    - Optionally add `network_id` if ids are not globally unique or if partitioning/indexing by network simplifies implementation.
  - Matching: on projection, compute `event_refs(e)` (the set of ids this event references, e.g., `channel_id`, `user_id`). For each `ref ∈ event_refs(e)`, execute handlers for rows where `(bulk_target_id, status) = (ref, active)`.
  - Idempotence: ensure bulk handlers are naturally idempotent, or track runs in `bulk_target_runs(bulk_target_id, targeted_by, event_id, executed_at, PRIMARY KEY(...))` if needed.
  - Deactivation: `UPDATE bulk_targets SET status=inactive, executed_at=now() WHERE ...` when the condition no longer applies.
- Modeling options (for clarity):
  - Local derived targets (recommended): registrations + post‑projection handlers; no wire events or DAG edges.
  - Local‑only journal (optional): record a private audit entry; still not shared; usually unnecessary.
  - Shared “target events” (not recommended): inflate the DAG and complicate ordering/authorization for a local concern.
- Scope and safety:
  - Targets may perform any local, deterministic side effects: indexing, background job scheduling (e.g., rekey), pre‑accounting for deletions, or gating classes of events. They must not mint new shared events or circumvent signature/authorization; any effect that influences accept/reject outcomes must be consistent with shared validation rules.
  - All durable target effects must be replay‑deterministic and idempotent.

Projector Lifecycle (Detailed)

- Overview: The projector handles every event via a simple, repeatable pipeline that preserves global ordering (DAG + blocking/toposort) and local derived actions (targets) without introducing new shared primitives.
- Steps (single event):
  1) Arrival (parse + record):
     - Parse bytes → event.
     - Record `recorded(event_id, recorded_by=local_peer_id)` for observability.
     - Optional on‑arrival target: default to ephemeral actions only (no durable writes). For events that are never intended to project (e.g., deletion suppression), limited durable cleanup on‑arrival is allowed if replay‑safe.
  2) Dependency check (block or proceed):
     - Evaluate declared DAG prerequisites for this event type (see Required Dependencies section).
     - If any are missing, place the event in a blocked queue keyed by the missing ids and return.
  3) Validation:
     - Verify signature via `signed_by` (resolve network/user/peer keys as specified).
     - Enforce authorization/time rules (e.g., signer is currently linked and not removed; subject is not deleted at `created_at`).
  4) Projection (atomic):
     - Write type‑specific rows (e.g., invites, users, admins, peers_shared).
     - Mark event valid (e.g., `valid_events(event_id)` or via successful insertions).
  5) Target dispatch (post‑projection):
     - If this event is a trigger for any registered targets (rows in `targets` where `target_id == id(trigger_evt)`), execute their handlers and mark executed (idempotent).
     - If this event declares a `target_id`:
       - If target is present/valid, execute the handler immediately.
       - Else insert registration into `targets` and return (handler will run when the target projects later).
  6) Unblocking cascade:
     - Re‑evaluate blocked events that depended on newly satisfied prerequisites.

- Targets table (local‑only, suggested schema):
  - `targets(target_id BLOB, targeted_by BLOB, status TEXT, executed_at INTEGER, PRIMARY KEY(target_id, targeted_by))`
  - `status ∈ {pending, executed}`; executed_at is a monotonic local timestamp (for audit).
  - Maintain simple counters: `targets_enqueued`, `targets_executed` for observability.

- Target registration (pseudocode):
  - `register_or_execute_target(src_evt)`
    - if `src_evt.target_id == None`: return
    - let `tid = src_evt.target_id`
    - if `trigger_is_present_and_valid(tid)`: `execute_target_once(src_evt, tid)`
      else: `INSERT OR IGNORE INTO targets(target_id=tid, targeted_by=src_evt.id, status=pending)`

- Target dispatch (pseudocode):
  - `on_project(trigger_evt)`:
    - for each row in `targets` where `target_id == id(trigger_evt)` AND `status=pending`:
      - load `src_evt = get_event(targeted_by)`
      - `execute_target_once(src_evt, target_id)`

- Bulk target dispatch (pseudocode):
  - `on_project(trigger_evt)`:
    - let `refs = event_refs(trigger_evt)`
    - for each `r in refs`:
      - for each row in `bulk_targets` where `(bulk_target_id, status) = (r, active)`:
        - load `src_evt = get_event(targeted_by)`
        - `execute_bulk_handler_once(src_evt, trigger_evt)`  # must be idempotent or tracked in bulk_target_runs

- Execute target once (idempotent core):
  - `execute_target_once(src_evt, tid)`:
    - `INSERT OR IGNORE INTO targets(target_id=tid, targeted_by=src_evt.id, status=pending)` (ensures row exists)
    - if row is already `executed`: return
    - switch on `src_evt.type`:
      - `channel_deleted`: mark rows for `channel_id=tid` deleted; retract indexes; enqueue idempotent cleanup jobs
      - `user_deleted`: mark rows for `user_id=tid` deleted; retract indexes
      - (extend with other local derived actions as needed)
    - `UPDATE targets SET status=executed, executed_at=now() WHERE (target_id, targeted_by)=(tid, src_evt.id)`

- Transaction and replay:
  - Prefer executing target handlers within the same DB transaction as the trigger’s projection (or immediately after) to keep atomicity.
  - On startup/sync catch‑up, re‑scan pending rows in `targets` and execute any whose triggers are projected; idempotence guarantees safe re‑runs.


Minimal Projector Rules (sketch)

- `peer_shared.project` (explicit steps):
  1) Ensure `invite(invite_id)` exists (declared DAG dependency).
  2) Load `inv = invites_peer[invite_id]` (by primary key).
  3) Verify signature: check `Verify(sig, canonical(peer_shared_body), inv.invite_pubkey) == true` (where `signed_by = invite_id` and `peer_shared_body` excludes only the signature field).
  4) Link:
     - INSERT OR IGNORE INTO `peers_shared(peer_shared_id=id(peer_shared), pubkey=peer_shared.pubkey)`.
     - INSERT OR IGNORE INTO `linked_peers(user_id=inv.user_id, peer_shared_id=id(peer_shared))`.
  5) Mark the event valid and unblock any dependents.
  - Rationale: the invite-key signature over the full event body replaces a separate acceptance event, simplifying ordering and state, and binds the invite usage to this exact peer_shared (id and contents).
  - `invite.project` (explicit steps):
    - kind=`user`:
      1) Dependencies: `network` (bootstrap); `peer_shared(signer)` and `admin_grant` (ongoing).
      2) Verify signature: bootstrap → signed_by=`network_id`; ongoing → signed_by=`peer_shared_id`.
      3) Consistency checks (ongoing):
         - Ensure `linked_peers` maps `peer_shared_id` → `signer_user_id`.
         - Ensure `admin(admin_grant).user_id == signer_user_id` AND `admin(admin_grant).network_id == this.network_id`.
      4) INSERT OR IGNORE `invites_user(invite_id, user_pubkey, signed_by, admin_grant)`; mark valid.
    - kind=`peer`:
      1) Dependencies: `user(subject)` always; `peer_shared(signer)` ongoing.
      2) Verify signature: first → signed_by=`user_id`; ongoing → signed_by=`peer_shared_id`.
      3) INSERT OR IGNORE `invites_peer(invite_id, user_id, invite_prekey_id, invite_pubkey, signed_by)`; mark valid.
- `user.project` (explicit steps):
  1) Ensure `invite(mode=user)` exists (declared DAG dependency).
  2) Load `inv_u = invites_user[invite_id]`.
  3) Verify signature: check `Verify(sig, canonical(user_body), inv_u.invite_pubkey) == true` (where `signed_by = invite_id` and `user_body` excludes only the signature field).
  4) INSERT OR IGNORE `users(user_id=id(user_event), user_pubkey=user_event.user_pubkey, ...)` and mark valid.
  5) Mark the event valid and unblock dependents.
  - Note: `user_pubkey` is the joiner's fresh keypair from the user event body, NOT `invite_pubkey`. One invite can create multiple users, each with unique identity.
- `admin.project` (explicit steps):
  1) Verify signature:
     - If signed_by=`network_id`: verify with `network_pubkey`.
     - If signed_by=`peer_shared_id`: verify with the peer public key from `peer_shared` (resolve by id).
  2) Subject dependency: ensure the subject `user` exists; if missing, block on `user_id`.
  3) If signed_by=`peer_shared_id` (ongoing):
     - Depend on the signer’s `peer_shared` (ensures link material is present); if missing, block on `peer_shared_id`.
     - Depend on `admin_grant` referencing prior `admin(signer_user)` that establishes the signer’s admin status (ordering via explicit dependency); block if missing.
     - Consistency checks: ensure `linked_peers` maps `peer_shared_id` → `signer_user_id`, and `admin(admin_grant).user_id == signer_user_id` AND `admin(admin_grant).network_id == this.network_id`.
  4) INSERT OR IGNORE `admins(network_id, user_id, created_at)`; mark valid and unblock dependents.

Removal Safety

- Do not use `user_pubkey` for ongoing ops after the first peer; discard it or keep it off‑device.
- Only peers (or user‑issued, short‑lived capabilities) authorize peer invites after first link; projector enforces “signer peer is currently linked”.
- On removal: delete `linked_peers` row and rotate group keys; removed peers cannot issue valid invites (not authorized) or decrypt new content.

Notes and Cross‑Refs

- This design aligns `first peer` with `later peers` through the same `invite(kind=peer) → peer_shared(proof)` pipeline.
- User identity is independent of peers: you can create a user without a peer, then attach peers uniformly.
- See `docs/ideal_protocol_design.md` for broader protocol context; this file specifies the minimal, no‑stubs linking model recommended there.
- The network key is used for bootstrapping only (`invite(mode=user)` + first `admin`), then discarded/offline; no explicit policy set is required.

## Connection Authorization

- Preferred (steady state): `signed_by = peer_shared_id`; verify with `peer_shared.pubkey` (post‑link).
- Bootstrap (pre‑link): `signed_by = invite_id`; verify with `invite(invite_id).invite_pubkey`; accept only if invite is present, valid for this network, and not expired/revoked.
- Signature coverage: must bind `network_id`, sender/recipient peer references, any transit material, and `connection_id` (the event_id).
- Nonce: use `connection_id` (event_id) as the nonce — replays are harmless due to content addressing/idempotent inserts, and nonces prevent state changes on replay.
- Scope: acceptance authorizes only transport (connection record, transit secret); DAG privileges still require the usual events and dependencies.

(see: Invitation; Joining (Graph); Event Signing)

## Plan: Aligning ideal_protocol_design.md

Goals

- Preserve the doc’s style/level of detail while aligning creation + joining.
- Express authorization/order strictly via DAG dependencies and signers.
- Unify on `signed_by` (polymorphic: network_id | user_id | peer_shared_id | invite_id).
- Remove invite_accepted; use `user` and `peer_shared` signed by `invite_id`.
- Add Targets section; integrate targets into deletion/removal as post‑projection side effects.

Steps

1) Terminology + Signing Model
- Update “Peer Creation” and “Event Signing”:
  - device → peer; created_by → signed_by.
  - signed_by polymorphism; verification by resolved signer type.
  - Exceptions are eliminated by typing: `signed_by = invite_id` for `user`, `peer_shared`.
  - (see: Joining (Graph) and Event Types)

2) Network Creation
- Replace first_peer + invite_accepted with explicit DAG:
  - `network → invite(user) [signed_by: network] → user [signed_by: invite_id] → admin [signed_by: network]`.
  - (see: Invitation; Joining (Graph))

3) Invitation
- Reframe user + peer invites with DAG + signers:
  - invite(user): bootstrap signed_by network; ongoing signed_by admin peer; depends_on network (bootstrap) | peer_shared(signer) + admin_grant (ongoing).
  - invite(peer): first signed_by user; ongoing signed_by linked peer; depends_on user (first) | user + peer_shared(signer) (ongoing).
  - (see: Joining (Graph); Event Types)

4) Joining (Graph)
- Replace invite_proof/invite_accepted:
  - Create User: invite(user) → user (signed_by=invite_id).
  - Link Peer: invite(peer) → peer_shared (signed_by=invite_id).
  - Add verification bullets; cross‑ref Signing Model + Required Dependencies.

5) Linking Peers on Multiple Devices
- Keep prekey/group_key_shared guidance; switch to invite(peer)+peer_shared proof.
- First by user; ongoing by linked peer (no admin required).
- (see: Invitation; Event‑Layer Encryption)

6) Authorization Model
- Replace any time‑based gating with DAG anchors:
  - Admin (ongoing): depends_on user(subject) + peer_shared(signer) + admin_grant.
  - invite(user) (ongoing): depends_on peer_shared(signer) + admin_grant.
  - invite(peer): no admin required; depends_on user (+ peer_shared signer for ongoing).
- Add “Authorization Dependencies Triplet” (user/peer/admin_grant) and matching rules.

7) Event Types
- Add concise per‑event blocks (Fields • Signed_by • Depends_on) for:
  - invite(user), user, invite(peer), peer_shared, admin.
- Remove invite_proof/invite_accepted references.

8) Blocking and Unblocking
- Reiterate DAG‑first; reference Required Dependencies.
- Remove local gating flags; ensure all ordering is via declared deps.

9) Targets (new) + Deletion/Removal integration
- New “Targets” section: local post‑projection side effects; no DAG edges; no effect on admissibility; bulk targets allowed.
- In Deletion/Removal: use targets for rekey/cleanup/index updates; admissibility remains signature + DAG.
  - (see: Targets; Deletion and Removal)

10) Connection (light touch)
- Note use of invite‑key proof from peer_shared if peer_shared not yet synced, or leave a TODO to align after Joining edits.


12) Consistency pass + diagrams + refs
- device→peer, created_by→signed_by; remove first_peer + invite_accepted; insert combined [dep]/[sig] mini diagrams; maintain anchors.

Policy toggles to enforce

- Only invite(user) ongoing is admin‑only; invite(peer) requires linked peer, no admin.
- Name: use `admin_grant` for the admin anchor.

## DAG Illustration (Combined, tagged edges)

Variant C — Grouped by phase

```
# Bootstrap-only — signers
[sig] invite(user) <== network
[sig] invite(peer) <== user
[sig] admin <== network

# Bootstrap-only — dependencies
[dep] network <== invite(user)
[dep] invite(user) <== user
[dep] user <== invite(peer)
[dep] invite(peer) <== peer_shared
[dep] admin <== user 

# Ongoing — signers
[sig] invite(user) <== admin_peer
 [sig] invite(peer) <== linked_peer
[sig] admin <== admin_peer

# Ongoing — dependencies
[dep] network <== invite(user)
[dep] invite(user) <== user
[dep] user <== invite(peer)
[dep] invite(peer) <== peer_shared
[dep] admin <== user
[dep] admin <== signer_peer_shared   # signer must be a linked peer
[dep] admin <== admin(signer_user)   # signer’s user must be admin (anchored by admin_grant)

# Optional explicit anchors (if used)
[dep] invite(user) <== admin_grant
[dep] admin <== admin_grant
```
```

Notes:
- Only the first admin and first user invites are signed by the network public key.
- Only the first peer invite must be signed by a user public key (later peer invites are signed by an already linked peer of that user).
- Network, invite, and user private keys are purged after final necessary use.
