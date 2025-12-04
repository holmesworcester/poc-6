# Signature Verification Fixes

## Problem

12 shareable event types are missing cryptographic signature verification in their `project()` functions. This is a security vulnerability - a malicious peer could forge events.

## Events Requiring Fixes

### HIGH Priority (can forge critical operations)

| File | Event Type | Current Issue |
|------|-----------|---------------|
| `events/content/message_deletion.py` | message_deletion | No verify_event() - could forge deletions |
| `events/identity/user_removed.py` | user_removed | No verify_event() - could forge user removal |
| `events/identity/peer_removed.py` | peer_removed | No verify_event() - could forge peer removal |

### MEDIUM Priority (data integrity issues)

| File | Event Type | Current Issue |
|------|-----------|---------------|
| `events/content/message_reaction.py` | message_reaction | No verify_event() - could forge reactions |
| `events/content/message_rekey.py` | message_rekey | No verify_event() - could forge rekeys |
| `events/identity/username_update.py` | username_update | No verify_event() - could forge names |
| `events/identity/network_name_update.py` | network_name_update | No verify_event() - could forge network names |
| `events/identity/peer_name_update.py` | peer_name_update | No verify_event() - could forge peer names |
| `events/network/observed_address.py` | observed_address | No verify_event() - could inject bad addresses |
| `events/content/message_reaction_deletion.py` | message_reaction_deletion | No verify_event() |
| `events/content/message_attachment.py` | message_attachment | No crypto verify (only signed_by field match) |
| `events/content/file_slice.py` | file_slice | No crypto verify (data integrity via hash though) |

## Pattern to Follow

Look at existing projectors that DO verify signatures. Example from `message.py`:

```python
def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    # ... get blob, unwrap ...
    event_data = crypto.parse_json(plaintext)

    # Get signer's public key
    signed_by = event_data.get('signed_by')
    signer_row = safedb.query_one(
        "SELECT public_key FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (signed_by, recorded_by)
    )
    if not signer_row:
        log.warning(f"message.project() signer not found: {signed_by[:20]}...")
        return None

    public_key = signer_row['public_key']

    # Verify signature
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"message.project() signature verification failed")
        return None

    # ... proceed with projection ...
```

## Helper Function

There's a helper `crypto.verify_signed_by_peer_shared(event_data, recorded_by, db)` in `crypto.py` that encapsulates the pattern above. Use it where appropriate:

```python
if not crypto.verify_signed_by_peer_shared(event_data, recorded_by, db):
    log.warning(f"<event>.project() signature verification failed")
    return None
```

## Implementation Steps

For each event type:

1. **Read the current project() function** to understand the flow
2. **Add signature verification** after parsing event_data, before any state changes
3. **Use verify_signed_by_peer_shared()** for standard peer-signed events
4. **Handle special cases**:
   - Network-signed events (check `signed_by == network_id`)
   - Self-signed events (rare)
5. **Return None** on verification failure (consistent with other projectors)
6. **Run tests** after each fix

## Order of Implementation

1. Start with HIGH priority (message_deletion, user_removed, peer_removed)
2. Then MEDIUM priority
3. Run full test suite after each batch

## Testing

```bash
PYTHONPATH=/home/hwilson/poc-6-sig-verify pytest /home/hwilson/poc-6-sig-verify/tests/ -v --tb=short
```

## Notes

- `message_attachment.py` and `file_slice.py` have non-standard signatures (take event_data param)
- They're called from `recorded.py` which doesn't verify before calling
- May need to add verification in the projector itself OR in recorded.py before dispatch
- `file_slice` may be lower priority since data integrity is via merkle hash

## Commit Format

```
Add signature verification to <event_type>.project()

Fixes Gotcha 15 violation - shareable events must verify signatures
before trusting event data to prevent forgery attacks.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <noreply@anthropic.com>
```
