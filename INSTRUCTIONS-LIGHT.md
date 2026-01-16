# INSTRUCTIONS-LIGHT

This is a short, low-risk set of v2 conversion tasks meant to be easy to follow.
Keep legacy `project()` intact and add v2 (`EVENT_SPEC` + `project_pure`) alongside it.

## Easy Events To Convert (Pick 1-3)

1) `events/identity/peer.py` (local-only, unsigned)
   - No signer, no deps.
   - `project_pure` should insert into `local_peers`:
     - `peer_id` = `ctx.event_id`
     - `public_key` = event_data `public_key` (base64 string)
     - `private_key` = base64-decoded bytes
     - `created_at` = event_data `created_at`

2) `events/group/group_prekey.py` (local-only, deterministic)
   - No signer, no deps.
   - `project_pure` should insert into `group_prekeys`:
     - `prekey_id` = `ctx.event_id`
     - `owner_peer_id` = `ctx.recorded_by`
     - `public_key` / `private_key` = base64-decoded bytes
     - `created_at` = `ctx.recorded_at`
     - `ttl_ms` = `created_at + GROUP_PREKEY_TTL_MS`

3) `events/network/observed_address.py` (signed, plaintext)
   - Add `signer_type` in `create()` (`'peer_shared'`).
   - `EVENT_SPEC` uses signer `{id_field:'signed_by', type_field:'signer_type'}`.
   - `project_pure` writes to `network_addresses` with event fields; no extra deps.

Optional: `events/network/self_address.py` is almost identical to `observed_address`.

## Minimal Testing Pattern (Parity)

Add a new file under `tests/projection_v2/` (e.g., `test_parity_peer_prekey_addresses.py`):

- Create two fresh in-memory DBs.
- Legacy path:
  - Store blob via `store.blob(...)`.
  - Call legacy `project(...)`.
- v2 path:
  - Call `resolve_event(...)`.
  - Call `project_pure(...)`.
  - Apply via `apply_writes(...)`.
- Compare table rows using `tests/projection_v2/helpers.get_table_rows`.

Keep tests small: one parity test per event is enough.

## Test Commands

Run after every change:

```
PYTHONPATH=/home/holmes/functional-projectors-and-commands \
pytest /home/holmes/functional-projectors-and-commands/tests/projection_v2 -v
```

Then run mixed scenario tests to exercise v2 + legacy together:

```
PYTHONPATH=/home/holmes/functional-projectors-and-commands \
pytest /home/holmes/functional-projectors-and-commands/tests/scenario_tests -v
```
