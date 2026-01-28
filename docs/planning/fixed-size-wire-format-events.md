# Fixed-Size Wire Event Layouts

This document defines the fixed-size (512-byte) wire format for all events.
It replaces JSON-on-the-wire and is designed for regular, LangSec-friendly parsing.

## Envelope (512 bytes)

```
0    1   version (u8)
1    1   type (u8)
2    1   flags (u8)
3    1   signer_type (u8)
4    4   count (u32, little-endian)          # global_count or 0
8    8   created_at_ms (u64, little-endian)
16   8   ttl_ms (u64, little-endian)
24   16  signer_id (16 bytes)                # 0 if unsigned
40   8   reserved (must be 0)
48   400 payload
448  64  signature (Ed25519)
```

Signature covers bytes 0-447 of the plaintext envelope (header + plaintext payload).
For encrypted payloads, signature is computed over plaintext before encryption.

## Flags (u8)

- bit 0: `encrypted` (0 = plaintext, 1 = wrapped payload)
- bit 1: `wrap_asym` (0 = symmetric, 1 = asymmetric; only if encrypted=1)
- bit 2: `unsigned` (1 = signature bytes must be zero)
- bits 3-7: reserved (0)

## Common Field Sizes

- `id16`: 16-byte event ID (BLAKE2b-128 of event bytes)
- `pubkey32`: Ed25519 public key
- `privkey32`: Ed25519 private key (seed)
- `secret32`: symmetric key
- `nonce24`: XChaCha20-Poly1305 nonce
- `tag16`: Poly1305 tag (appended to ciphertext)
- `ip16`: IPv6 bytes (IPv4 stored as v4-mapped)
- `len16`: u16 length for UTF-8 strings or variable bytes

All integers are little-endian. All fixed-length byte fields are zero-padded.
String fields use `len16 + bytes[N]` (UTF-8) with zero padding.

## Payload Wrapping (when flags.encrypted=1)

Payload is 400 bytes. Wrapping is fixed-size:

- `key_id` (16) || `wrap_data` (384)
  - symmetric: `nonce24` || `ciphertext+tag` (360)
    - max plaintext = 400 - 56 = 344 bytes
  - asymmetric: `ephemeral_pubkey32` || `nonce24` || `ciphertext+tag` (328)
    - max plaintext = 400 - 88 = 312 bytes

## Signer Type (u8)

```
0 = none
1 = peer_shared
2 = user
3 = invite
4 = network
5 = peer (local-only)
```

## Event Type Codes (u8)

```
0x01 message
0x02 channel
0x03 message_update
0x04 message_deletion
0x05 message_reaction
0x06 message_reaction_deletion
0x07 message_attachment
0x08 file_slice
0x09 message_rekey
0x0A channel_update

0x10 group
0x11 group_member
0x12 group_key
0x13 group_key_shared
0x14 group_prekey
0x15 group_prekey_shared

0x20 user
0x21 username_update
0x22 user_removed
0x23 peer
0x24 peer_shared
0x25 peer_name_update
0x26 peer_removed
0x27 network
0x28 network_name_update
0x29 admin
0x2A invite
0x2B invite_accepted

0x30 connection_prekey
0x31 connection_prekey_shared
0x32 connection_request
0x33 connection_ack
0x34 self_address
0x35 observed_address
0x36 network_intro
0x37 negentropy
```

## Wire-Only Status (No JSON Fallback)

The following events are now emitted and parsed only in fixed-size wire format:

- admin
- channel
- channel_update
- connection_ack
- connection_prekey
- connection_prekey_shared
- connection_request
- file_slice
- group
- group_key
- group_key_shared
- group_member
- group_prekey
- group_prekey_shared
- invite
- invite_accepted
- message
- message_attachment
- message_deletion
- message_reaction
- message_reaction_deletion
- message_rekey
- message_update
- negentropy
- network
- network_intro
- network_name_update
- observed_address
- peer
- peer_name_update
- peer_removed
- peer_shared
- self_address
- user
- user_removed
- username_update

## Fixed String Sizes

- `NAME_MAX` = 64 bytes
- `CONTENT_MAX` = 256 bytes
- `UPDATE_MAX` = 256 bytes
- `FILENAME_MAX` = 128 bytes
- `MIME_MAX` = 32 bytes

## Event Payloads (Plaintext Layouts)

For encrypted events, the following layouts describe the plaintext that is wrapped.
For plaintext events, the layout is stored directly in the payload.

### message (0x01, encrypted sym)

- channel_id (id16)
- author_id (id16)
- disappearing_time_ms (u64)
- content_len (len16)
- content_bytes (CONTENT_MAX)
- pad (to 344 bytes)

### channel (0x02, encrypted sym)

- group_id (id16)
- name_len (len16)
- name_bytes (NAME_MAX)
- disappearing_time_ms (u64)
- is_main (u8)
- admin_grant_id (id16, 0 if none)
- pad

### message_update (0x03, encrypted sym)

- message_id (id16)
- group_id (id16)
- edited_by (id16)
- author_id (id16)
- new_content_len (len16)
- new_content_bytes (UPDATE_MAX)
- pad

### message_deletion (0x04, encrypted sym)

- message_id (id16)
- pad

### message_reaction (0x05, encrypted sym)

- message_id (id16)
- reactor_id (id16)
- emoji_utf32 (u32)
- pad

### message_reaction_deletion (0x06, encrypted sym)

- reaction_id (id16)
- pad

### message_attachment (0x07, encrypted sym)

- message_id (id16)
- file_id (id16)
- blob_bytes (u64)
- total_slices (u32)
- nonce_prefix (20 bytes)
- enc_key (secret32)
- root_hash (32 bytes)
- filename_len (len16)
- filename_bytes (FILENAME_MAX)
- mime_len (len16)
- mime_bytes (MIME_MAX)
- pad

### file_slice (0x08, special 512-byte layout)

File slices stay 512 bytes but do not use the common envelope. Layout:

```
0    1   version
1    1   type (0x08)
2    16  file_id
18   4   slice_number (u32)
22   24  nonce24
46   450 ciphertext
496  16  tag16
```

No signature. Integrity is verified via message_attachment.root_hash.
Notes:
- Ciphertext is padded with zero bytes to 450 bytes for fixed layout; plaintext is trimmed to
  `blob_bytes` on reassembly.

### message_rekey (0x09, plaintext)

- original_message_id (id16)
- new_key_id (id16)
- new_ciphertext_len (len16)
- new_ciphertext_bytes (max 366 bytes: ciphertext only)
- pad
Notes:
- Nonce is derived deterministically from `original_message_id || new_key_id` (24 bytes).

### channel_update (0x0A, encrypted sym)

- channel_id (id16)
- group_id (id16)
- updated_by (id16)
- new_channel_name_len (len16)
- new_channel_name_bytes (NAME_MAX)
- new_disappearing_time_ms (u64)
- pad

### group (0x10, encrypted sym)

- name_len (len16)
- name_bytes (NAME_MAX)
- key_id (id16)
- is_main (u8)
- network_id (id16, 0 if none)
- pad

### group_member (0x11, encrypted sym)

- group_id (id16)
- user_id (id16)
- added_by (id16)
- admin_grant_id (id16, 0 if none)
- pad

### group_key (0x12, plaintext)

- key (secret32)
- pad

### group_key_shared (0x13, encrypted asym)

- key_id (id16)
- symmetric_key (secret32)
- recipient_prekey_id (id16)
- pad (to 312 bytes)

### group_prekey (0x14, plaintext)

- public_key (pubkey32)
- private_key (privkey32)
- pad

### group_prekey_shared (0x15, plaintext)

- group_prekey_id (id16)
- peer_id (id16)
- public_key (pubkey32)
- pad

### user (0x20, plaintext)

- invite_id (id16)
- user_pubkey (pubkey32)
- network_id (id16, 0 if none)
- pad

### username_update (0x21, encrypted sym)

- user_id (id16)
- name_len (len16)
- name_bytes (NAME_MAX)
- pad

### user_removed (0x22, plaintext)

- removed_user_id (id16)
- pad

### peer (0x23, plaintext, local-only)

- public_key (pubkey32)
- private_key (privkey32)
- pad

### peer_shared (0x24, plaintext)

- public_key (pubkey32)
- peer_id (id16)
- invite_id (id16)
- pad

### peer_name_update (0x25, encrypted sym)

- peer_id (id16)
- name_len (len16)
- name_bytes (NAME_MAX)
- pad

### peer_removed (0x26, plaintext)

- removed_peer_shared_id (id16)
- pad

### network (0x27, plaintext)

- network_pubkey (pubkey32)
- pad

### network_name_update (0x28, encrypted sym)

- network_id (id16)
- name_len (len16)
- name_bytes (NAME_MAX)
- pad

### admin (0x29, plaintext)

- user_id (id16)
- network_id (id16)
- admin_grant_id (id16, 0 if none)
- pad

### invite (0x2A, plaintext)

- mode (u8)                # 0=user, 1=peer
- invite_pubkey (pubkey32)
- invite_prekey_id (id16)
- group_id (id16, 0 if none)
- channel_id (id16, 0 if none)
- key_id (id16, 0 if none)
- network_id (id16, 0 if none)
- inviter_peer_shared_id (id16, 0 if none)
- inviter_user_id (id16, 0 if none)
- target_user_id (id16, 0 if none; mode=peer)
- admin_grant_id (id16, 0 if none)
- inviter_ip (ip16)
- inviter_port (u16)
- pad

### invite_accepted (0x2B, plaintext, local-only)

- invite_id (id16)
- invite_prekey_id (id16)
- invite_private_key (privkey32)
- inviter_peer_shared_id (id16)
- network_id (id16)
- channel_id (id16)
- key_id (id16)
- inviter_connection_prekey_public_key (pubkey32)
- inviter_connection_prekey_shared_id (id16)
- inviter_connection_prekey_id (id16)
- inviter_ip (ip16)
- inviter_port (u16)
- link_user_id (id16, 0 if none)
- inviter_peer_shared_blob_id (id16, 0 if none)
- pad

### connection_prekey (0x30, plaintext, local-only)

- public_key (pubkey32)
- private_key (privkey32)
- pad

### connection_prekey_shared (0x31, plaintext)

- connection_prekey_id (id16)
- peer_id (id16)
- public_key (pubkey32)
- pad

### connection_request (0x32, plaintext)

- key (secret32)
- to_peer_shared_id (id16, 0 if none)
- invite_id (id16, 0 if none)
- pad

### connection_ack (0x33, plaintext)

- for_request_id (id16)
- key (secret32)
- pad

### self_address (0x34, plaintext)

- peer_id (id16)
- ip (ip16)
- port (u16)
- pad

### observed_address (0x35, plaintext)

- observed_peer_id (id16)
- ip (ip16)
- port (u16)
- pad

### network_intro (0x36, plaintext)

- peer1_id (id16)
- peer2_id (id16)
- pad

### negentropy (0x37, plaintext)

Fixed-size union for sync messages:

- connection_id (id16)
- reply_connection_id (id16)
- msg_type (u8)            # 1=range_request, 2=range_matched, 3=range_events
- range_id (8 bytes)       # 64-bit random
- level (u8)               # 0=root, 1=prefix_2, 2=prefix_4, 3=prefix_6
- prefix_len (u8)          # bytes used in prefix_bytes (0..3)
- prefix_bytes (3 bytes)   # unified_key prefix bytes, zero-padded
- hash_bytes (16 bytes)    # their hash (range_request) or our_hash (range_events)
- root_hash (16 bytes)
- total_events (u32)
- parent_range_id (8 bytes) # 0 if none
- event_id_count (u8)
- event_ids (N * id16)     # N = 15 (fixed slots)
- pad

Notes:
- For msg_type != range_events, event_id_count = 0 and event_ids are zeroed.
- For msg_type = range_matched, hash_bytes is zeroed and prefix_len is 0.
