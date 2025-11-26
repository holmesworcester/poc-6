# Deletion Finalization Plan

## Current State

### ✅ Working (19 tests passing)
- Message self-deletion
- Message rekey (forward secrecy)
- User/peer removal
- TTL-based purging
- Key purging after deletion

### ⚠️ Has Issues
| Feature | Issue | Tests |
|---------|-------|-------|
| Message admin deletion | Multi-peer sync - deletion events not syncing | XFAIL |
| Message unauthorized check | Same sync issue | XFAIL |

---

## Phase 1: Fix Message Deletion Multi-Peer Sync

**Goal:** Make `test_message_deletion_admin` and `test_message_deletion_unauthorized` pass.

### Investigation

1. **Debug sync issue**
   - Why don't deletion events reach other peers?
   - Check if deletion events are added to `shareable_events`
   - Check if `recorded.project()` handles `message_deletion` type

2. **Files to check:**
   - `events/content/message_deletion.py` - Does `project()` mark events as shareable?
   - `events/network/recorded.py` - Does it dispatch to message_deletion.project()?
   - `tests/scenario_tests/test_message_deletion.py` - What exactly fails?

### Fix

1. Ensure deletion events are marked shareable after projection
2. Ensure deletion events are included in sync
3. Verify authorization on remote projection (use blocking if invalid)

### CLI Demo

- Add `delete-message <n>` command
- Add `purge` command to run forward secrecy cycle
