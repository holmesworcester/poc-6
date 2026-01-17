# Implementation Guide: Trust-Anchored Bootstrap + Connection Causality

This guide describes the intended implementation behavior and maps it to the
TLA+ model in `docs/tla/BootstrapGraph.tla`.

Goals
- Use a single trust anchor mechanism for all joins, including bootstrap.
- Prevent force-validating invites; validity must follow the dependency chain.
- Make connection bootstrap causality explicit and testable.

Trust Anchor Rules (TLA: Guard(Net), InvNetAnchor)
- The network event is only valid after an `invite_accepted` has established a
  trust anchor for that network.
- `invite_accepted` must include `network_id` for all invite types (user and
  peer). This is the only trusted bridge from out-of-band link data.
- Invites are never force-validated. They become valid only after their signer
  is valid (network or peer_shared).

Bootstrap Isomorphic Flow (TLA: Deps + Guard(Net))
- The bootstrap path should accept its own invite link to create
  `invite_accepted` before the network becomes valid.
- Sequence, in principle:
  1) Create network event blob (self-signed for integrity, not trust).
  2) Create bootstrap invite (mode=user) signed by the network key.
  3) Accept the invite locally to create `invite_accepted` with `network_id`.
  4) Network validates (trust anchor) and unblocks invite -> user -> peer invite
     -> peer_shared.

Connection Bootstrap Causality (TLA: InvConnReq/InvConnAck/InvConnInvite/InvConnPeer)
- `connReq` (invite-authenticated request) requires:
  - `invite_accepted` valid, and
  - the invite data recorded (so the invite pubkey can be derived or fetched).
- `connAck` is only valid after `connReq` is accepted.
- `connInvite` (active invite-labeled connection) requires `connAck`.
- `connPeer` (upgraded peer-labeled connection) requires both peers' `peer_shared`.

Implementation checklist
- Ensure `invite_accepted` always records `network_id` and writes
  `trust_anchors(network_id, recorded_by)`.
- Gate network validity on the trust anchor (do not auto-validate the network
  for the bootstrap creator).
- Keep invite validation strictly dependent on signer validity; no
  `valid_events` inserts for invites.
- Enforce connection ordering: request -> ack -> invite-connection ->
  peer-connection.

Test alignment
- Use the invariants in `docs/tla/BootstrapGraph.tla` as acceptance criteria.
- Add blocking/unblocking tests for:
  - network validity without trust anchor (should block),
  - invite validity without network (should block),
  - connection ack without request (should fail),
  - connection upgrade without both peer_shared (should fail).

Schema-level checks
- `docs/tla/EventGraphSchema.tla` encodes the full event graph (with mode
  variants) and signer/dependency relationships. Use it to validate that
  any implementation changes preserve causal ordering across the entire
  graph, not just bootstrap.
