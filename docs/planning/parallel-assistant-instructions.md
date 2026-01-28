# Parallel Assistant Instructions: Fixed-Size Wire Events

Context: We are moving all events to a fixed 512-byte wire format (see `docs/planning/fixed-size-wire-format-events.md`). JSON-on-the-wire is being removed; for now, wire emission is gated behind per-event env flags to keep scenario tests green.

## Assigned Events (Assistant)

Implement wire format for these 16 events:

- message_reaction (0x05)
- message_reaction_deletion (0x06)
- message_attachment (0x07)
- file_slice (0x08, special 512-byte layout, no envelope)
- message_rekey (0x09)
- channel_update (0x0A)
- group (0x10)
- group_member (0x11)
- group_key (0x12)
- group_key_shared (0x13)
- group_prekey (0x14)
- group_prekey_shared (0x15)
- connection_prekey (0x30)
- connection_prekey_shared (0x31)
- connection_request (0x32)
- connection_ack (0x33)

## Assigned Events (Main Assistant)

I will handle these 16 events:

- user (0x20)
- username_update (0x21)
- user_removed (0x22)
- peer (0x23)
- peer_shared (0x24)
- peer_name_update (0x25)
- peer_removed (0x26)
- network (0x27)
- network_name_update (0x28)
- admin (0x29)
- invite (0x2A)
- invite_accepted (0x2B)
- self_address (0x34)
- observed_address (0x35)
- network_intro (0x36)
- negentropy (0x37)

## Implementation Pattern (Follow This)

1) `core/wire_format.py`
- Add constants (event type, plaintext size, max lengths).
- Add `encode_*_plaintext` / `decode_*_plaintext`.
- Add `_encrypt_*_payload` / `_decrypt_*_payload` (symmetric unless spec says otherwise).
- Add `encode_*_wire_event` / `decode_*_wire_event` that use `WireHeader` + `_signing_bytes`.
- Add `is_wire_*_envelope` (except `file_slice`, which is special).

2) `core/recorded.py`
- Detect wire envelopes early (before JSON unwrap) and call the new decode functions.

3) Event module (`events/content/*` or `events/group/*` or `events/network/*`)
- Add env flag: `QUIET_WIRE_<EVENT>` (uppercase event name).
- Add `_wire_shadow_*` validation: roundtrip plaintext encode/decode to validate field sizes.
- In `create(...)`, if flag enabled, build blob via wire encoder and call `store.event(...)`.
  Otherwise keep `store.publish(...)` path.
- In `project_pure(...)`, call `_wire_shadow_*` so parsing failures surface in scenario tests.

4) Tests
- Add small roundtrip tests in `tests/test_wire_format.py` for plaintext and encrypt/decrypt.

## Notes

- Signature verification: include `_wire_signature` and `_wire_signed_bytes` in `decode_*_wire_event` so `resolver` verifies signatures for events that declare `EVENT_SPEC['signer']`.
- For global counters (e.g., LWW updates), store `global_count` in `WireHeader.count` (u32).
- For `file_slice`: follow the spec layout exactly (no envelope, no signature).
- Run scenario tests after each batch: `pytest -v tests/scenario_tests/`.
