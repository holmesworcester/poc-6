# Forward Secrecy CLI Implementation Plan

## Goal

Add a `keys` command to the CLI that displays the state of group keys and prekeys for demonstrating forward secrecy. Shows which keys are active vs pending purge, with message/key counts. Once purged, keys disappear - their absence IS the proof of forward secrecy.

## Mockup

```
> keys
KEYS (alice - desktop):
  group_keys:
    1. key_Dh4zXt - active (2 messages)
    2. key_Bf2xQr - active (1 message)
    3. key_Ak9mNp - pending_purge (0 messages)

  prekeys:
    1. prekey_3Fk2Nm - active (0 group_keys)
    2. prekey_7Jt9Wp - active (2 group_keys)

> purge-keys
✓ rekeyed 0 messages
✓ purged key_Ak9mNp
✓ purged prekey_8Hs4Ry (no remaining group_keys)

> keys
KEYS (alice - desktop):
  group_keys:
    1. key_Dh4zXt - active (2 messages)
    2. key_Bf2xQr - active (1 message)

  prekeys:
    1. prekey_3Fk2Nm - active (0 group_keys)
    2. prekey_7Jt9Wp - active (1 group_keys)

> keys --summary
KEYS (alice - desktop):
  group_keys: 2 active, 0 pending_purge
  prekeys: 2 active
```

## Forward Secrecy Chain

```
prekey (private) → decrypts → group_key_shared → contains → group_key → decrypts → message
```

For complete forward secrecy, we must purge both:
1. **Group key** - so messages can't be decrypted
2. **Prekey** - so the group_key_shared event can't be decrypted to recover the group key

## Current State

### Tables Available

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `group_keys` | Active symmetric keys | `key_id`, `recorded_by` |
| `keys_to_purge` | Keys marked for purging | `key_id`, `marked_at`, `recorded_by` |
| `messages` | Messages with key reference | `message_id`, `key_id`, `recorded_by` |
| `message_deletions` | Deleted message tracking | `message_id`, `deleted_by`, `recorded_by` |
| `group_prekeys` | Local peer's prekeys | `prekey_id`, `owner_peer_id`, `recorded_by` |
| `group_keys_shared` | Key sharing events | `key_shared_id`, `original_key_id`, `recorded_by` |

### Current Problem

`group_keys_shared` doesn't track which prekey was used for wrapping - it's only in the blob's crypto hint. We need this to:
1. Count group_keys per prekey for display
2. Cascade prekey purge when all its group_keys are purged

---

## Implementation Steps

### Step 1: Add `recipient_prekey_id` to `group_keys_shared`

**File**: `events/group/group_key_shared.sql`

```sql
-- Add column to track which prekey was used for wrapping
ALTER TABLE group_keys_shared ADD COLUMN recipient_prekey_id TEXT;

CREATE INDEX IF NOT EXISTS idx_group_keys_shared_by_prekey
    ON group_keys_shared(recipient_prekey_id, recorded_by);
```

**Update**: `events/group/group_key_shared.py:project()`

Extract prekey_id from blob hint and store it:

```python
# Extract recipient_prekey_id from blob (first 32 bytes is the hint/prekey_id)
recipient_prekey_id = crypto.b64encode(blob[:crypto.ID_SIZE])

safedb.execute(
    """INSERT OR IGNORE INTO group_keys_shared
       (key_shared_id, original_key_id, recipient_prekey_id, signed_by, created_at, recorded_by, recorded_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)""",
    (key_shared_id, computed_key_id, recipient_prekey_id, ...)
)
```

### Step 2: Add `prekeys_to_purge` Table

**File**: `events/group/group_prekey.sql`

```sql
-- Prekeys marked for purging (all group_keys shared via them are purged)
CREATE TABLE IF NOT EXISTS prekeys_to_purge (
    prekey_id TEXT NOT NULL,
    marked_at INTEGER NOT NULL,
    recorded_by TEXT NOT NULL,
    PRIMARY KEY (prekey_id, recorded_by)
);
```

### Step 3: Add `group_key.list()` Function

**File**: `events/group/group_key.py`

```python
def list(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all group keys with status and message counts.

    Returns keys in two states:
    - active: in group_keys, not in keys_to_purge
    - pending_purge: in group_keys AND in keys_to_purge

    Once purged, keys are gone (not shown).

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of dicts with: key_id, status, message_count, created_at
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    keys = safedb.query(
        """SELECT gk.key_id, gk.created_at,
                  CASE WHEN ktp.key_id IS NOT NULL THEN 'pending_purge' ELSE 'active' END as status,
                  (SELECT COUNT(*) FROM messages m
                   WHERE m.key_id = gk.key_id AND m.recorded_by = ?) as message_count
           FROM group_keys gk
           LEFT JOIN keys_to_purge ktp ON gk.key_id = ktp.key_id AND ktp.recorded_by = gk.recorded_by
           WHERE gk.recorded_by = ?
           ORDER BY gk.created_at DESC""",
        (peer_id, peer_id)
    )

    return list(keys)
```

### Step 4: Add `group_prekey.list()` Function

**File**: `events/group/group_prekey.py`

```python
def list(peer_id: str, t_ms: int, db: Any) -> list[dict[str, Any]]:
    """List all group prekeys with status and group_key counts.

    Status:
    - active: has private key, not marked for purge
    - pending_purge: marked for purge (all group_keys purged)

    Args:
        peer_id: Local peer ID
        t_ms: Current time for TTL check
        db: Database connection

    Returns:
        List of dicts with: prekey_id, status, group_key_count, created_at
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    prekeys = safedb.query(
        """SELECT gp.prekey_id, gp.created_at,
                  CASE WHEN ptp.prekey_id IS NOT NULL THEN 'pending_purge' ELSE 'active' END as status,
                  (SELECT COUNT(*) FROM group_keys_shared gks
                   WHERE gks.recipient_prekey_id = gp.prekey_id AND gks.recorded_by = ?) as group_key_count
           FROM group_prekeys gp
           LEFT JOIN prekeys_to_purge ptp ON gp.prekey_id = ptp.prekey_id AND ptp.recorded_by = gp.recorded_by
           WHERE gp.recorded_by = ? AND gp.owner_peer_id = ?
           ORDER BY gp.created_at DESC""",
        (peer_id, peer_id, peer_id)
    )

    return list(prekeys)
```

### Step 5: Update Purge Cycle for Cascading Prekey Purge

**File**: `events/content/message_deletion.py`

Update `run_message_purge_cycle()` to also check and purge prekeys:

```python
def run_message_purge_cycle(peer_id: str, t_ms: int, db: Any) -> dict[str, Any]:
    # ... existing key purge logic ...

    # After purging group_keys, check for prekeys that can be purged
    # A prekey can be purged if ALL group_keys shared via it are now purged
    prekeys_purged = []

    # Find prekeys where all their group_keys are gone
    orphaned_prekeys = safedb.query(
        """SELECT DISTINCT gks.recipient_prekey_id as prekey_id
           FROM group_keys_shared gks
           WHERE gks.recorded_by = ?
           AND NOT EXISTS (
               SELECT 1 FROM group_keys gk
               WHERE gk.key_id = gks.original_key_id AND gk.recorded_by = ?
           )
           AND NOT EXISTS (
               SELECT 1 FROM group_keys_shared gks2
               JOIN group_keys gk ON gks2.original_key_id = gk.key_id AND gk.recorded_by = gks2.recorded_by
               WHERE gks2.recipient_prekey_id = gks.recipient_prekey_id AND gks2.recorded_by = ?
           )""",
        (peer_id, peer_id, peer_id)
    )

    for row in orphaned_prekeys:
        prekey_id = row['prekey_id']
        # Delete private key from group_prekeys
        safedb.execute(
            "DELETE FROM group_prekeys WHERE prekey_id = ? AND recorded_by = ?",
            (prekey_id, peer_id)
        )
        prekeys_purged.append(prekey_id)
        log.info(f"run_message_purge_cycle() purged prekey {prekey_id[:20]}...")

    stats['prekeys_purged'] = len(prekeys_purged)
    return stats
```

### Step 6: Add CLI `keys` Command

**File**: `cli.py`

```python
from events.group import group_key, group_prekey

def cmd_keys(session: CLISession, summary: bool = False):
    """Display key state for forward secrecy demo."""
    account = session.get_selected_account()

    # Get group keys
    keys = group_key.list(account.peer_id, session.db)

    # Get prekeys
    prekeys = group_prekey.list(account.peer_id, session.current_time_ms, session.db)

    if summary:
        display_keys_summary(account, keys, prekeys)
    else:
        display_keys_full(account, keys, prekeys)


def display_keys_full(account: AccountContext, keys: list, prekeys: list):
    """Display full key state."""
    print(f"KEYS ({account.full_name}):")

    print("  group_keys:")
    if not keys:
        print("    (no keys)")
    else:
        for i, k in enumerate(keys, 1):
            key_id_short = k['key_id'][:10]
            status = k['status']
            msg_count = k['message_count']
            print(f"    {i}. key_{key_id_short} - {status} ({msg_count} messages)")

    print()
    print("  prekeys:")
    if not prekeys:
        print("    (no prekeys)")
    else:
        for i, pk in enumerate(prekeys, 1):
            prekey_id_short = pk['prekey_id'][:10]
            status = pk['status']
            key_count = pk['group_key_count']
            print(f"    {i}. prekey_{prekey_id_short} - {status} ({key_count} group_keys)")


def display_keys_summary(account: AccountContext, keys: list, prekeys: list):
    """Display summary key state."""
    print(f"KEYS ({account.full_name}):")

    active_keys = sum(1 for k in keys if k['status'] == 'active')
    pending_keys = sum(1 for k in keys if k['status'] == 'pending_purge')

    active_prekeys = sum(1 for p in prekeys if p['status'] == 'active')
    pending_prekeys = sum(1 for p in prekeys if p['status'] == 'pending_purge')

    print(f"  group_keys: {active_keys} active, {pending_keys} pending_purge")
    print(f"  prekeys: {active_prekeys} active, {pending_prekeys} pending_purge")
```

Add to command dispatcher in `execute_command()`:

```python
elif cmd == "keys":
    summary = "--summary" in parts
    cmd_keys(session, summary=summary)

elif cmd == "purge-keys":
    cmd_purge_keys(session)

elif cmd == "delete-message":
    if len(parts) < 2:
        print("usage: delete-message <n>")
    else:
        cmd_delete_message(session, int(parts[1]))
```

### Step 7: Add CLI `delete-message` Command

**File**: `cli.py`

```python
from events.content import message_deletion

def cmd_delete_message(session: CLISession, message_num: int):
    """Delete a message by number."""
    account = session.get_selected_account()

    if not session.selected_channel_id:
        print("✗ no channel selected")
        return

    # Get messages to find the one to delete
    messages = message.list(session.selected_channel_id, account.peer_id, session.db)

    if not (1 <= message_num <= len(messages)):
        print(f"✗ message #{message_num} not found")
        return

    msg = messages[message_num - 1]
    message_id = msg['message_id']

    deletion_id = message_deletion.create(
        peer_id=account.peer_id,
        message_id=message_id,
        t_ms=session.current_time_ms,
        db=session.db
    )

    session.db.commit()
    session.current_time_ms += 100

    print(f"✓ deleted message")
    print(f"✓ marked key for purging")
    print()

    session.run_auto_tick()
    display_state(session)
```

### Step 8: Add CLI `purge-keys` Command

**File**: `cli.py`

```python
def cmd_purge_keys(session: CLISession):
    """Run forward secrecy purge cycle."""
    account = session.get_selected_account()

    stats = message_deletion.run_message_purge_cycle(
        peer_id=account.peer_id,
        t_ms=session.current_time_ms,
        db=session.db
    )

    session.db.commit()
    session.current_time_ms += 100

    if stats['messages_rekeyed'] > 0:
        print(f"✓ rekeyed {stats['messages_rekeyed']} messages")
    if stats['keys_purged'] > 0:
        print(f"✓ purged {stats['keys_purged']} keys")
    if stats.get('prekeys_purged', 0) > 0:
        print(f"✓ purged {stats['prekeys_purged']} prekeys")
    if stats['errors']:
        for err in stats['errors']:
            print(f"⚠ {err}")
    if stats['messages_rekeyed'] == 0 and stats['keys_purged'] == 0:
        print("✓ no keys to purge")
    print()

    session.run_auto_tick()
    display_state(session)
```

### Step 9: Update Help Text

**File**: `cli.py`

Add to help output:
```python
print("  keys [--summary]")
print("  delete-message <n>")
print("  purge-keys")
```

### Step 10: Add Tests

**File**: `tests/scenario_tests/test_forward_secrecy_cli.py`

```python
def test_keys_display():
    """Test that keys command shows correct state."""
    # Create network, send messages
    # Verify keys shows active keys with message counts

def test_delete_and_purge():
    """Test delete-message and purge-keys flow."""
    # Create network, send messages
    # Delete a message
    # Verify key marked for purging (pending_purge)
    # Run purge
    # Verify key is gone from list

def test_cascading_prekey_purge():
    """Test that prekeys are purged when all their group_keys are purged."""
    # Create network with multiple users (prekeys consumed)
    # Delete all messages using keys shared via a prekey
    # Run purge
    # Verify both group_key AND prekey are gone
```

---

## Task Checklist

- [ ] 1. Add `recipient_prekey_id` column to `group_keys_shared` table
- [ ] 2. Update `group_key_shared.project()` to extract and store prekey_id
- [ ] 3. Add `prekeys_to_purge` table to `group_prekey.sql`
- [ ] 4. Add `group_key.list()` function
- [ ] 5. Add `group_prekey.list()` function
- [ ] 6. Update `run_message_purge_cycle()` to cascade prekey purge
- [ ] 7. Add `cmd_keys()` to CLI with display functions
- [ ] 8. Add `cmd_delete_message()` to CLI
- [ ] 9. Add `cmd_purge_keys()` to CLI
- [ ] 10. Update CLI help text and command dispatcher
- [ ] 11. Add scenario tests for forward secrecy CLI
- [ ] 12. Manual testing with demo scenario

## Demo Scenario

```
> new-network --name alice --device desktop
> send "Secret message 1"
> send "Secret message 2"
> send "Secret message 3"
> keys
> keys --summary
> delete-message 1
> keys
> purge-keys
> keys
```

Expected flow:
1. Initial: Shows active keys with message counts
2. After delete: Key moves to `pending_purge` status
3. After purge: Key disappears from list (forward secrecy achieved)
