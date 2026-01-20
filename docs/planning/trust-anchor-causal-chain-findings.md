# Trust Anchor Causal-Chain Findings

This document summarizes current gaps in the trust-anchor/causal-chain model for joining/linking.
The intended model is:

- `invite_accepted` is a trust anchor marker, not validity.
- A trust anchor should make a future matching `network` event eligible for validation.
- Events signed by a signer (`network`, `user`, `peer_shared`) must depend on the signer event that carries the signer’s public key, so signature verification is always possible when projection runs.
- Validity should cascade via dependency resolution, not via direct inserts into `valid_events`.

## Observed Gaps (Current Branch)

1) `invite_accepted.project()` directly inserts into `valid_events` for both `network_id` and `invite_id`.
   - File: `events/identity/invite_accepted.py`
   - Effect: Trust anchor becomes immediate validity, bypassing verification.
   - Side effect: Unblocks dependents before signer events are projected.

2) `invite.project()` skips signature verification when `network_id` is already in `valid_events`.
   - File: `events/identity/invite.py`
   - Effect: `invite(mode=user)` can project without access to the network signer’s public key.
   - This violates the “must depend on signer event” rule.

3) `user.project()` and `peer_shared.project()` derive invite pubkeys from `invite_accepteds` when the invite event is not projected.
   - Files: `events/identity/user.py`, `events/identity/peer_shared.py`
   - Effect: User/peer_shared can project without the invite being valid/verified.
   - This bypasses the signer dependency and eliminates the causal chain.

4) Legacy dependency logic still treats `peer_shared` as `NO_DEPS_TYPES`.
   - File: `events/network/recorded.py`
   - Effect: The legacy path does not enforce “peer_shared signed_by invite” dependency.

## Consequences

- Validity is conflated with trust anchor eligibility.
- Signature verification can be skipped or delayed until after projection.
- Cascades may occur out of order, creating hidden invariants and brittle behavior.

## Desired Model (Restated)

- `invite_accepted` records trust anchor intent only.
- `network` should become valid only when the network event is present and verifiable.
- `invite(mode=user)` should block until network event is valid (public key known).
- `user` and `peer_shared` should block until the invite event is valid.
- `invite(mode=peer)` should block until the signer event is valid (user or peer_shared).

## Next Steps (for implementation)

- Introduce a trust-anchor table or flag separate from `valid_events`.
- Remove direct inserts into `valid_events` in `invite_accepted.project()`.
- Remove “valid via trust anchor” skip in `invite.project()`.
- Remove invite_pubkey derivation from `invite_accepteds` in `user.project()` and `peer_shared.project()`.
- Update dependency resolution to ensure signer event is always a blocking dependency.


## Additional Inspection Notes

Current code still treats trust anchor as validity in multiple paths:

- `events/identity/invite_accepted.py`:
  - Directly inserts `network_id` and `invite_id` into `valid_events`.
  - Immediately unblocks dependents, which bypasses signer verification and the causal chain.

- `events/identity/invite.py`:
  - If `network_id` is already in `valid_events`, it skips signature verification for bootstrap invites.
  - This lets `invite(mode=user)` project without access to the network signer’s public key.

- `events/identity/user.py` and `events/identity/peer_shared.py`:
  - Both derive `invite_pubkey` from `invite_accepteds` when the invite event is not projected.
  - This allows these events to project without the invite being valid.

- `events/network/recorded.py` legacy path:
  - `peer_shared` remains in `NO_DEPS_TYPES`, so the legacy dependency checker can skip the invite dependency.

These paths should be aligned with the intended model: trust anchor marks eligibility, validity comes only after signer event projection and signature verification.

## Specific Problem Statement

The trust anchor is still treated as immediate validity. As a result, events that must block on signer events (to verify signatures with signer public keys) can project without the signer event being present. This breaks the dependency cascade and hides ordering bugs.

Concrete breakpoints:

- `/home/holmes/functional-projectors-and-commands/events/identity/invite_accepted.py`
  - Inserts `network_id` and `invite_id` directly into `valid_events`.
  - Unblocks downstream events before signer verification.

- `/home/holmes/functional-projectors-and-commands/events/identity/invite.py`
  - Skips signature verification when `network_id` is already “valid via trust anchor”.
  - Allows `invite(mode=user)` to project without the signer’s public key.

- `/home/holmes/functional-projectors-and-commands/events/identity/user.py` and
  `/home/holmes/functional-projectors-and-commands/events/identity/peer_shared.py`
  - Derive invite pubkeys from `invite_accepteds` if the invite event is missing.
  - Allows `user` / `peer_shared` to project without the invite event being valid.

- `/home/holmes/functional-projectors-and-commands/events/network/recorded.py`
  - Legacy dependency logic still includes `peer_shared` in `NO_DEPS_TYPES`.

Net effect: the causal chain is violated because signer‑dependent events don’t reliably block until the signer event is valid and verified.
