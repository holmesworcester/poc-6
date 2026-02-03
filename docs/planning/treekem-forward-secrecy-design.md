# TreeKEM Forward Secrecy: Extending Purge/Rekey

## Existing Flow (Protocol Spec §Forward Secrecy)

```
1. Message deleted/expired → mark key_id in keys_to_purge
2. run_message_purge_cycle():
   - For each key in keys_to_purge:
     - Find all messages using that key
     - Rekey them to a new clean key (message_rekey)
     - Delete old secret from secrets table
   - Find orphaned pubkeys (all secrets shared to them are gone)
   - Delete orphaned pubkeys from pubkeys table
3. Keys purged → forward secrecy achieved
```

## Current Implementation

**message_deletion.py** (lines 193-208):
- On delete, marks `key_id` in `keys_to_purge`

**run_message_purge_cycle()** (lines 406-589):
- Processes `keys_to_purge`, rekeys messages, deletes secrets
- Finds orphaned pubkeys via `secrets_shared.recipient_pubkey_id`
- Deletes orphaned pubkeys from `pubkeys` table

## What's Missing

The purge chain for TreeKEM distribution:

```
message → secret (sender key) → distribution method:
  ├─ pubkey (O(n) leaf fallback) ✓ HANDLED
  ├─ treekem_pubkey (O(log n) tree cover) ✗ NOT HANDLED
  └─ treekem_secret/root (O(1) broadcast) ✗ NOT HANDLED
```

### Gap 1: TreeKEM Pubkeys Not Purged

`secrets_shared.recipient_pubkey_id` can reference EITHER `pubkeys` OR `treekem_pubkeys`, but orphan detection only deletes from `pubkeys`.

### Gap 2: TreeKEM Secrets Not Purged

When secrets are distributed via `secret_broadcast`, the root secret (`treekem_secrets` at depth=0) is used. These should be purged when all broadcasts using them are gone.

## Extension Design

### 1. Extend orphan detection to treekem_pubkeys

Add after existing pubkey orphan purge in `run_message_purge_cycle()`:

```python
# Find orphaned treekem_pubkeys
# A treekem_pubkey is orphaned when:
# - No secrets_shared references it (secret distribution)
# - No treekem_secrets_shared references it (path secret distribution)
orphaned_treekem_pubkeys = safedb.query("""
    SELECT DISTINCT tp.treekem_pubkey_id
    FROM treekem_pubkeys tp
    WHERE tp.recorded_by = ?
    -- No secrets distributed to this pubkey
    AND NOT EXISTS (
        SELECT 1 FROM secrets_shared ss
        JOIN secrets s ON ss.original_secret_id = s.secret_id
            AND s.recorded_by = ss.recorded_by
        WHERE ss.recipient_pubkey_id = tp.treekem_pubkey_id
        AND ss.recorded_by = tp.recorded_by
    )
    -- No path secrets distributed to this pubkey
    AND NOT EXISTS (
        SELECT 1 FROM treekem_secrets_shared tss
        JOIN treekem_secrets ts ON tss.treekem_secret_id = ts.treekem_secret_id
            AND ts.recorded_by = tss.recorded_by
        WHERE tss.recipient_pubkey_id = tp.treekem_pubkey_id
        AND tss.recorded_by = tp.recorded_by
    )
""", (peer_id,))

for row in orphaned_treekem_pubkeys:
    safedb.execute(
        "DELETE FROM treekem_pubkeys WHERE treekem_pubkey_id = ? AND recorded_by = ?",
        (row['treekem_pubkey_id'], peer_id)
    )
```

### 2. Extend orphan detection to treekem_secrets

Add after treekem_pubkey purge:

```python
# Find orphaned treekem_secrets (path secrets)
# A treekem_secret is orphaned when:
# - No secret_broadcast uses it (directly or via root derivation)
# - Its treekem_update has been superseded by a newer one
orphaned_treekem_secrets = safedb.query("""
    SELECT DISTINCT ts.treekem_secret_id
    FROM treekem_secrets ts
    WHERE ts.recorded_by = ?
    -- No broadcasts use this tree's secrets
    AND NOT EXISTS (
        SELECT 1 FROM secrets_broadcast sb
        JOIN secrets s ON sb.secret_id = s.secret_id
            AND s.recorded_by = sb.recorded_by
        WHERE sb.source_update_id = ts.source_update_id
        AND sb.recorded_by = ts.recorded_by
    )
""", (peer_id,))

for row in orphaned_treekem_secrets:
    safedb.execute(
        "DELETE FROM treekem_secrets WHERE treekem_secret_id = ? AND recorded_by = ?",
        (row['treekem_secret_id'], peer_id)
    )
```

### 3. Track source_update_id in treekem_secrets

Need to ensure `treekem_secrets` tracks which update they belong to:

```sql
-- Check if column exists, add if not
ALTER TABLE treekem_secrets ADD COLUMN source_update_id TEXT;
```

And populate it in `treekem_secret.create()`.

## Purge Chain Summary

| Key Type | Purge Trigger | Orphan Condition |
|----------|---------------|------------------|
| `secret` | Message deleted | In `keys_to_purge`, all messages rekeyed |
| `pubkey` | Secret purged | No `secrets_shared` references it |
| `treekem_pubkey` | Secret purged | No `secrets_shared` OR `treekem_secrets_shared` references it |
| `treekem_secret` | Broadcast secrets purged | No `secrets_broadcast` uses its update |

## Implementation Steps

### Step 1: Add source_update_id to treekem_secrets schema
Track which update each path secret belongs to.

### Step 2: Populate source_update_id on creation
Update `treekem_secret.create()` to set `source_update_id`.

### Step 3: Extend run_message_purge_cycle()
Add orphan detection for `treekem_pubkeys` and `treekem_secrets`.

### Step 4: Update stats tracking
Add `treekem_pubkeys_purged` and `treekem_secrets_purged` to stats.

## Testing

1. Create message with secret distributed via treekem_pubkey
2. Delete message → triggers rekey
3. Run purge cycle → verify secret purged
4. Run purge cycle again → verify treekem_pubkey purged (now orphaned)

5. Create message with secret distributed via broadcast (root secret)
6. Delete message → triggers rekey
7. Run purge cycle → verify secret purged
8. Run purge cycle again → verify treekem_secret purged (now orphaned)

## Notes

- **No TTL needed**: Purging is driven by message deletion, not time
- **No epoch-based purging**: Users keep history access; epochs control who gets NEW keys
- **Cascading orphans**: Purging secrets may orphan pubkeys, which may orphan path secrets
- **Multiple cycles**: May need multiple purge cycles to cascade through the chain
