# Document Missing Events in Spec

## Issue

The implementation has 19 event types not documented in the spec's Appendix A:

- `admin` - Admin authorization grants
- `group_key`, `group_key_shared` - Group encryption keys
- `group_member` - Group membership (replaces spec's `grant`)
- `group_prekey`, `group_prekey_shared` - Group prekey rotation
- `invite_accepted` - Local-only invite state
- `message_attachment` - File attachments
- `message_reaction`, `message_reaction_deletion` - Emoji reactions
- `message_rekey` - Per-message rekeying for forward secrecy
- `message_update` - Message editing
- `network` - Network root identity
- `network_name_update` - Network renaming
- `observed_address` - Peer address observations
- `peer`, `peer_shared` - Peer identity
- `peer_name_update` - Peer display names
- `username_update` - Username changes
- `self_address` - Self address announcements
- `sync_connect` - Sync connection state
- `transit_key`, `transit_prekey`, `transit_prekey_shared` - Transit routing keys

## Location

`docs/quiet-protocol-specification.md` - Appendix A (Types and Layouts)

## Fix

Add these event types to Appendix A with their field layouts.
