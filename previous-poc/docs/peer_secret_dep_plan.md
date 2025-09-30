# ES-only identity signing via peer_secret dep

Status: draft
Owner: protocol/quiet
Updated: 2025-09-25

## Problem

The signature handler currently reaches into a local projection table to fetch a private key (historically `peer_locals`, recently `peer_secrets`). This breaks the “pure handlers” goal and couples signing to database projections. It also leaves identity ownership implicit instead of explicit in envelopes.

We want to:
- Require `peer_secret_id` as a dependency for any event that must be signed.
- Have resolve_deps supply the signer material (private key) from the Event Store (ES), decrypting the local-only `peer_secret` event with the process root secret.
- Update the signature handler to consume only `resolved_deps` (no direct DB reads), and to never depend on the `peer_secrets` table.

## Current state (as of this doc)

- Local identities are emitted as `peer_secret` events (visibility `local-only`). Their plaintext is stored root-encrypted under the process-local `root_secret`.
- A projector inserts `peer_secret` into a local `peer_secrets` table for convenience (private_key by public_key, and list of owned identities).
- The signature handler:
  - For `peer` events: signs using the public key in the event; no private key lookup.
  - For other events: finds the public key via `peers(peer_id)->public_key`, then queries `peer_secrets` by public_key to get the private key.
- resolve_deps intentionally excludes `peer_secret_id` from the allowlist to avoid blocking on local-only secrets.

## Goals

- Pure signing: signature handler uses only `resolved_deps` input; no DB reads.
- Explicit actor: self-created, signed events declare `peer_secret_id` in `deps`.
- Non-blocking local secrets: resolve_deps resolves local-only secrets immediately (or returns local error), never blocks on them.
- Keep verification model: signature verification continues to rely on `peer` dependency to obtain the public key (already handled via deps/projections), not the private key.

## Proposed design

1) Flows include `peer_secret_id` in `deps` for any self-created, signed event types:
   - `peer` (self-attested intro)
   - `network`, `group`, `channel`, `message`, `address`, `user`, `member`, etc.
   - Rule of thumb: if the validator expects a signature and the event is emitted locally, add the signer `peer_secret_id` to `deps`.

2) resolve_deps support for local-only identity secrets:
   - Allow `peer_secret_id` in dep extraction.
   - For each `peer_secret_id` dep, try to load the corresponding ES row:
     - SELECT the `event_blob` from `events` where `event_id = peer_secret_id` and `visibility = 'local-only'`.
     - Decrypt or parse using the existing root-secret path (helper already exists in `event_decrypt`).
     - On success, add to `resolved_deps` as `peer_secret:<id>` with minimally: `{ public_key, private_key, created_at }`.
     - If missing or decryption fails: mark as a local error (do not block). The envelope should be dropped by downstream since deps cannot be satisfied locally.
   - Do NOT require presence in `validated_events` for `peer_secret_id` (they are local-only and should not participate in cross-event unblocking). Treat them as immediate lookup.

3) Signature handler changes:
   - Remove DB access. For signing, look up the signer’s private key from `resolved_deps["peer_secret:<id>"]` where `<id>` is the `peer_secret_id` declared in `deps`.
   - Continue to use `peer` dependency (resolved_deps or event plaintext for `peer` events) for public-key verification.
   - If `peer_secret` dep is missing, set a clear error (`missing_signer_private_key`) and drop the envelope.

4) Event store visibility tracking:
   - When recording `seen_events`, continue to attribute ingestion to the local identity. If a `peer_secret_id` is present on the envelope, prefer it for attribution.
   - This removes reliance on `peer_secrets` for “ownership” checks. Ownership is implicit: a `peer_secret` exists in ES and can be decrypted with root secret.

5) Optional: keep or remove `peer_secrets` projection
   - Short term: keep the projector/table to avoid changing jobs/queries immediately.
   - Medium term: migrate jobs (e.g., sync scheduler) to enumerate owned identities by scanning ES for `peer_secret` (local-only) rather than joining `peer_secrets`.
   - Long term: remove `peer_secrets` projection once no code path depends on it.

## Handler changes in detail

- resolve_deps
  - Add `peer_secret_id` to allowed IDs or special-case handling.
  - Implement a local-only resolver:
    - If a dep is `peer_secret:<id>` or `<id>` was extracted as a `peer_secret_id`, fetch ES row and parse/decrypt with root secret.
    - Insert into `resolved_deps` under key `peer_secret:<id>`.
    - Do not touch `blocked_events` for missing `peer_secret` deps; return `missing_deps=False` and annotate an error for local-only failure.

- signature
  - Signing path: require `peer_secret:<id>` in `resolved_deps`; extract `private_key` bytes; sign canonical event (without signature field).
  - Verification path: unchanged logic to derive public key from `peer` dependency; no DB queries.

## Flow changes (representative)

- peer.create
  - Currently avoids adding `peer_secret_id` to deps to prevent blocking. With the new resolver behavior, include it in `deps`.

- network.create, group.create, channel.create, message.create, user.create/join_as_user, address.announce
  - Ensure `deps` contains the local `peer_secret_id` used to sign. **how do we get it?**
  - No other flow logic needs to change; encryption already uses `key_secret_id` deps.

## Validation and ordering

- Signing runs after resolve_deps so the signer private key is available.
- Validators stay pure; they still check signature presence/structure; signature handler sets `sig_checked`.

## Migration plan

1) Implement resolver augmentation and signature changes behind a feature flag (optional) or all at once if tests cover flows.
2) Update flows to add `peer_secret_id` to deps.
3) Update tests to assert:
   - Envelopes declare `peer_secret_id` in deps for signed events.
   - Signature handler does not read DB and fails with `missing_signer_private_key` if dep is absent.
4) Migrate any jobs that rely on `peer_secrets` to ES-only.
5) Remove `peer_secrets` table and projector when no longer used (or keep as optimization cache if desired).

## Risks / tradeoffs

- Slight CPU overhead to parse/decrypt the signer `peer_secret` during dependency resolution. Mitigations: cache resolver results for the request_id lifetime; the handler already keeps `resolved_deps` in the envelope.
- Flow authors must remember to add `peer_secret_id` to deps for any self-created signed event.
- If root-secret encryption is disabled for `peer_secret` in some environments, ensure the resolver can handle plaintext local-only blobs as well.

## Open questions

- Should resolve_deps require `validated_events` presence for `peer_secret`? Proposed: no; they are local-only and should resolve immediately.
- Naming: use `peer_secret:<id>` as `resolved_deps` key to mirror `key_secret:<id>` and `prekey_secret:<id>`.
- Should we drop DB attribution in event_store seen gating and rely only on `peer_secret_id` on the envelope when present? Proposed: yes; fallback to peers table only if absent.

## Acceptance criteria

- No direct DB reads in signature handler for private keys.
- resolve_deps can provide signer private key in `resolved_deps` given a `peer_secret_id` dep.
- All self-created signed flows include `peer_secret_id` in deps.
- Scenario tests pass: creating a network, sending a message, syncing, etc., without relying on `peer_secrets` table.

## Test plan

- Unit: signature handler signs when `resolved_deps[peer_secret:<id>]` is present; errors otherwise.
- Unit: resolve_deps returns `{public_key, private_key}` for a local `peer_secret` event id.
- Integration: scenario tests (single-user, multi-identity) exercise new deps and show no DB access paths in signature.

## Work items

- [ ] resolve_deps: add local-only resolver for `peer_secret_id` → {public_key, private_key}
- [ ] signature: remove DB reads; consume `peer_secret:<id>` from `resolved_deps`
- [ ] flows: add `peer_secret_id` to deps for all self-created signed event types
- [ ] tests: update and add coverage for the new dependency and purity guarantees
- [ ] optional: migrate sync job to derive “owned identities” from ES
- [ ] optional: remove `peer_secrets` table / projector once no longer referenced
