Remaining Events: Simplicity + Explicit Side Effects
====================================================

Goal: capture where we can simplify remaining event flows while keeping
causal chains explicit and debuggable. These notes lean toward making
state-changing side effects visible as events when practical, and keeping
exceptions small and explicit.

1) Group key rotation
---------------------
Current: `group_key.rotate_for_removal()` creates a new `group_key` event,
updates `groups.key_id` directly, then emits `group_key_shared` to members.
That direct `groups` table update is an implicit side effect.

Simpler/explicit options:
- Add `group_key_rotated` (or `group_update`) event:
  - payload: group_id, new_key_id, signed_by (admin/peer_shared).
  - projection: update `groups.key_id`.
  - dependencies: new_key_id valid, group_id valid, signer valid.
  - cascade: add `event_dependencies` child=group_key_rotated, parent=group_key.
- Keep rotation as "just create a newer group_key event" but derive the
  active key by query (latest key per group). That avoids the write
  side effect but may complicate lookup and requires deterministic ordering.
- Sender-driven rotation: rotate on send if key is "dirty" (e.g.,
  member removed or key marked for purge), then emit an explicit
  `group_key_rotated` event. Avoid doing this as a hidden side effect
  inside `group.pick_key()` (read path mutations are hard to debug).

2) Marking keys for purge
-------------------------
Current: `message_deletion.project()` inserts into `keys_to_purge`
(local table) as a side effect. This is a derived state from deletions.

Simpler/explicit options:
- Keep `keys_to_purge` as a derived materialization (local-only) and
  treat it as a cache; no new event needed.
- Or add a local-only `key_purge_requested` event emitted as a side
  effect of deletion, then project into `keys_to_purge`. This makes
  reprojection deterministic and the trigger explicit while still not
  syncing purge intent across peers.

3) Message rekey
----------------
Current: `message_rekey` is a shareable event; projection mutates the
existing message blob and updates `messages.key_id`. This preserves the
original message_id so attachments/reactions stay linked, but it is a
special-case "mutation" side effect.

Options:
- Keep current behavior for simplicity in references. The mutation is the
  explicit exception; document it as such.
- Alternative: rekey as "new message + deletion/tombstone". This avoids
  in-place mutation but breaks references and requires mapping old->new
  ids (extra tables and migration logic).
- If we want purity but stable ids: allow `message_rekey` to be the sole
  mutation exception and document why (references + indexing).

4) Cascading deletion
---------------------
We already track cascades via `event_dependencies`. That makes it cheap
to enforce "delete parent => delete children".

Gaps to tighten:
- Group key rotation: if no explicit `group_key_rotated` event, the
  dependency chain from the new key back to the group is implicit.
- Attachments: file slices depend on `file_id`, but `file_id` is not a
  first-class event. Cascades from message -> attachment -> slices will
  not traverse unless we anchor `file_id` to an event.
- Consider adding explicit parent links where we currently write to
  tables directly (e.g., group key rotation).

5) Connection + network layer events
------------------------------------
Network events are already event-sourced with `EVENT_SPEC`. The "exception"
is the trust-anchor gating (validity upon arrival), which is intentional.

Connection/sync events are exceptional because they are ephemeral and
processed on the transit path (no store/recorded wrappers). You can add
`EVENT_SPEC` for validation, but they still need an ephemeral pipeline.
This is a larger integration step and may be worth postponing until
other event chains are stable.

6) Message attachments
----------------------
Message attachments are encrypted + signed, and file slices are unsigned
with integrity via `root_hash`. This is good, but the dependency chain is
not fully explicit:
- `message_attachment` depends on `message`.
- `file_slice` depends on `file_id`, which is not an event.

Simpler/explicit options:
- Introduce a `file` event (local or shareable) keyed by `file_id`, and
  make `file_slice` depend on it. The `file` event can be created as a
  side effect of `message_attachment.create()` and projected deterministically.
- Or embed `attachment_id` in slices to create a real event dependency
  chain: message -> attachment -> slices.
- If we keep as-is, note that cascades won't remove slices and rely on
  TTL/purge for cleanup.

Summary: simplest default rule
------------------------------
If a side effect changes durable state (keys, group key pointers, purge
tracking), it is simplest to make it an explicit event with a projector,
unless the flow is ephemeral or purely local caching. This keeps causal
chains visible and makes reprojection deterministic.

Questions for Codex High
------------------------
1) Should group key rotation be an explicit event (group_key_rotated) or
   a derived "latest key" query?
2) Is `keys_to_purge` best modeled as a local event, or as a derived view
   from deletions?
3) Is `message_rekey` as the sole mutation exception acceptable, or should
   we pursue a "new message + tombstone" model?
4) Should we add a `file` event (or tie slices to attachment_id) so file
   slices participate in cascades?
5) Are connection/sync events worth bringing into the v2 spec now, or do
   we keep them explicitly exceptional?

Suggested prompt to send:
"Review the attached notes on remaining event flows (group key rotation,
key purge, message rekey, attachments, connection/network exceptions).
Which simplifications are most valuable, and where do you see hidden
complexity or missing causal links? Please comment on whether to make
side effects explicit events vs derived tables."
