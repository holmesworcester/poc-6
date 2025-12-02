# Slim Invite Event for UDP-Safe Size

## Problem

The ongoing `invite` event (mode='user') is ~830 bytes, which exceeds the 600-byte UDP-safe packet limit. After transit wrapping overhead (112 bytes for double encryption), only 488 bytes are available for the signed event JSON.

**Current invite event structure (lines 283-306 in events/identity/invite.py):**
```python
invite_event_data = {
    'type': 'invite',
    'mode': mode,
    'invite_pubkey': invite_pubkey_b64,
    'invite_prekey_id': invite_prekey_id,
    'network_id': network_id,
    'group_id': all_users_group_id,
    'channel_id': channel_id,
    'key_id': key_id,
    'inviter_peer_shared_id': peer_shared_id,
    'inviter_user_id': inviter_user_id,
    'inviter_transit_prekey_public_key': crypto.b64encode(inviter_prekey_public_key),
    'inviter_transit_prekey_shared_id': inviter_transit_prekey_shared_id,
    'inviter_transit_prekey_shared_created_at': inviter_transit_prekey_shared_created_at,
    'inviter_transit_prekey_id': inviter_prekey_id,
    'address': inviter_ip,
    'port': inviter_port,
    'signed_by': peer_shared_id,
    'created_at': t_ms,
    'admin_grant': admin_grant_id  # optional
}
```

## Key Insight

The system has TWO data paths for invites:
1. **Invite EVENT** (synced across network via UDP) - MUST fit 488 bytes
2. **Invite LINK** (out-of-band QR/URL) - NO size limit

Many fields in the invite event are only needed for initial bootstrap contact and can move to the invite link.

## Solution: Field Analysis

### Fields to KEEP (~407 bytes)

| Field | Bytes | Why Required |
|-------|-------|--------------|
| `type` | 16 | Event routing |
| `mode` | 14 | Flow control (user vs peer) |
| `invite_pubkey` | 60 | User signs with this - needed for user.project() verification |
| `invite_prekey_id` | 44 | Crypto hint for GKS decryption - MUST sync |
| `group_id` | 36 | Core semantic - where to add user |
| `inviter_user_id` | 42 | Pure functional projection - admin_grant verification without 2nd-degree deps |
| `signed_by` | 36 | Signature verification + inviter peer identity |
| `created_at` | 27 | Event ordering |
| `admin_grant` | 38 | Authorization chain - validators need this |
| `signature` | 98 | Event signature |
| JSON overhead | 20 | Structure |

### Fields to REMOVE (~423 bytes saved)

| Field | Bytes | Why Can Remove |
|-------|-------|----------------|
| `inviter_peer_shared_id` | 48 | **REDUNDANT** with `signed_by` (both = peer_shared_id for ongoing invites) |
| `network_id` | 38 | Derivable: `groups.network_id` from `group_id` |
| `channel_id` | 38 | Discoverable via sync after joining |
| `key_id` | 34 | Derivable: `groups.key_id` from `group_id` |
| `inviter_transit_prekey_public_key` | 76 | Bootstrap only - move to link |
| `inviter_transit_prekey_shared_id` | 56 | Bootstrap only - move to link |
| `inviter_transit_prekey_shared_created_at` | 51 | Bootstrap only - move to link |
| `inviter_transit_prekey_id` | 50 | Bootstrap only - move to link |
| `address` | 20 | Already in link (`invite_link_data['ip']`) |
| `port` | 12 | Already in link (`invite_link_data['port']`) |

## Target Structure

```python
# AFTER: Minimal invite event (~407 bytes)
invite_event_data = {
    'type': 'invite',
    'mode': mode,
    'invite_pubkey': invite_pubkey_b64,
    'invite_prekey_id': invite_prekey_id,
    'group_id': group_id,
    'inviter_user_id': inviter_user_id,
    'signed_by': peer_shared_id,
    'created_at': t_ms,
    'admin_grant': admin_grant_id,
}

# Invite link (out-of-band, no size limit) - add transit prekey fields
invite_link_data = {
    'invite_blob': invite_blob_b64,
    'invite_id': invite_id,
    'invite_prekey_id': invite_prekey_id,
    'invite_private_key': crypto.b64encode(invite_private_key),
    'inviter_peer_shared_id': peer_shared_id,
    'inviter_peer_shared_blob': inviter_peer_shared_blob_b64,
    'network_id': network_id,
    'ip': inviter_ip,
    'port': inviter_port,
    # NEW: Transit prekey fields moved here
    'inviter_transit_prekey_public_key': crypto.b64encode(prekey_public),
    'inviter_transit_prekey_shared_id': prekey_shared_id,
    'inviter_transit_prekey_id': prekey_id,
}
```

## Files to Modify

1. **events/identity/invite.py**
   - `create()`: Remove fields from invite_event_data, keep in invite_link_data
   - `project()`: Remove transit prekey projection (lines 598-612)
   - Update `create_bootstrap_user_invite()` if needed

2. **events/identity/user.py**
   - `join()`: Read transit prekey from `invite_data` (link) not `invite_blob` (event)
   - Project transit prekey from link data during join

3. **events/identity/invite.py** (pure-functional branch alignment)
   - Update `project()` to use `signed_by` as inviter_id (remove inviter_peer_shared_id fallback)

## Notes

- Bootstrap invites (signed_by=network_id) are already small (~350 bytes) - no changes needed
- For bootstrap, `inviter_peer_shared_id` differs from `signed_by`, but those invites are small
- The 81-byte margin (488 - 407) provides room for future fields if needed
