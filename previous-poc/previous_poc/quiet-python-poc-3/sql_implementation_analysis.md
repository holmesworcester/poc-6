# SQL Implementation Analysis vs docs.md

## Overview
The signed_groups protocol has been converted from dict-based state to SQL-only implementation. Let me analyze how it works now vs the original design.

## 1. Event Storage and Processing

### Original Design (docs.md):
- "single shared state that all identities can modify by creating and projecting an event in a single transaction"
- All commands create events and project them immediately
- No distinct receiving step

### Current SQL Implementation:
✅ **MATCHES**: Events are stored in `event_store` table and projected in the same transaction
- Each handler has a projector that processes events
- Commands use `run_command()` which creates events and projects them atomically
- Example from message/create.py:
  ```python
  # Creates event
  message_event = {
      'type': 'message',
      'id': message_id,
      'channel_id': channel_id,
      'author_id': user_id,
      'peer_id': peer_id,
      'user_id': user_id,
      'content': content,
      'text': content,
      'signature': signature
  }
  # Event is automatically projected by run_command
  ```

## 2. Blocking Mechanism

### Original Design (docs.md):
- `blocked` is a table listing blocked `event-id` with blocked-by `event-id`
- Events referencing unseen events are blocked
- Events signed by non-users are blocked
- `blocked.unblock` is called after projection to unblock dependent events

### Current SQL Implementation:
❌ **DIFFERS**: The blocking mechanism has changed significantly
- No `blocked` table with `event-id` and `blocked-by` pairs
- Instead uses `recheck_queue` table with columns: `event_id`, `reason_type`, `available_at_ms`
- When a dependency arrives (user, group, etc.), it inserts "recheck_all" into recheck_queue
- `blocked/process_unblocked.py` then:
  1. Deletes ALL entries from recheck_queue
  2. Reprocesses ALL events from event_store
  3. This means no selective unblocking - everything is reprocessed

## 3. Event Validation and Signatures

### Original Design (docs.md):
- All events have real signatures of plaintext
- Events signed by non-users are blocked
- Various validation rules per event type

### Current SQL Implementation:
✅ **MOSTLY MATCHES**: Signature validation is implemented
- Uses dummy signatures in format `dummy_sig_signed_by_{signer_id}`
- Projectors validate signatures match claimed signers
- Example from message/projector.py:
  ```python
  if signature.startswith("dummy_sig_signed_by_"):
      signer = signature.replace("dummy_sig_signed_by_", "")
      if signer != str(peer_id):
          return db  # Reject mismatched signature
  ```
- Special handling for "dummy_sig_from_unknown" which is always rejected

## 4. Event Types and Their Rules

### User Events:
✅ **MATCHES**:
- Valid if signed with network creator's pubkey (first user)
- Valid if they have a valid invite from existing user
- Stored in `users` table with network_id, pubkey, name, invite_id

### Group Events:
✅ **MATCHES**:
- Must be created by valid user
- Creator must match signer
- Stored in `groups` table

### Add Events:
✅ **MATCHES**:
- Adds existing network member to group
- Validates group exists, user exists, adder exists
- Stored in `adds` table

### Message Events:
✅ **MATCHES**:
- Validates signature matches peer_id
- Validates peer is linked to user (or peer_id == user_id for self)
- Stored in `messages` table
- Group membership validation NOT implemented (docs say messages from non-group members should be hidden)

### Link/Link-Invite Events:
✅ **MATCHES**:
- Link invites allow multiple devices per user
- Links associate peer_id with user_id
- Proper validation of signatures

## 5. Permutation Testing

### Original Design (docs.md):
- "all tests attempt all permutations of events to ensure they end in the same projected state"

### Current SQL Implementation:
✅ **MATCHES**: Test runner automatically generates permutations
- For tests with multiple events, all orderings are tested
- Ensures eventual consistency regardless of event order

## 6. Key Differences from Original Design

1. **Blocking Mechanism**: 
   - Original: Selective blocking with blocked_by relationships
   - Current: Global reprocessing when any dependency arrives
   - This is why "Partial unblock" test fails

2. **State Storage**:
   - Original: Dict-based state
   - Current: Pure SQL tables
   - No more `db['users']` access, only SQL queries

3. **Group Membership Validation**:
   - Not implemented for messages (docs say non-group member messages should be hidden)
   - Would need additional SQL joins to validate

4. **Event Store**:
   - Added protocol-specific `event_store` table
   - Stores complete event history for reprocessing

## 7. Transaction Handling

✅ **IMPROVED**: Better transaction atomicity
- All projectors removed direct `conn.commit()` calls
- Framework manages transactions via `with_retry()`
- Ensures all-or-nothing event processing

## Summary

The SQL implementation largely follows the original design with these exceptions:
1. **Blocking mechanism is simplified** - no selective unblocking
2. **Group membership validation for messages not implemented**
3. **Pure SQL instead of dict state** - more robust but less flexible

The core protocol logic remains intact: events are validated, signed, and projected atomically. The main compromise is in the blocking/unblocking mechanism which now reprocesses everything rather than selectively unblocking dependent events.