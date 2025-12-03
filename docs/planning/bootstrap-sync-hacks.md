# Bootstrap/Sync Hacks & Technical Debt

Inventory of workarounds, shortcuts, and implicit dependencies in the sync, sync_connect, and bootstrap code.

## 1. PENDING Placeholder String ✅ FIXED

**Pattern**: Using literal string `'PENDING'` as a placeholder for `peer_shared_id` before it exists.

| Location | Code | Status |
|----------|------|--------|
| ~~`user.py:310`~~ | ~~`peer_shared_id='PENDING'`~~ | ✅ Now uses `None` |
| ~~`user.py:329`~~ | ~~`'inviter_peer_shared_id': 'PENDING'`~~ | ✅ Now uses `None` |
| ~~`user.py:343`~~ | ~~`INSERT INTO peer_self ... 'PENDING'`~~ | ✅ Removed, handled by peer_shared.project() |
| ~~`user.py:625-631`~~ | ~~Creates `PENDING` peer_self entry~~ | ✅ Removed |
| ~~`sync_connect.py:55,64-65,74`~~ | ~~Special-cases `PENDING`~~ | ✅ Now checks for truthy value |
| ~~`recorded.py:185`~~ | ~~Skips dep check for `'PENDING'`~~ | ✅ Removed |

**Resolution**: Replaced with `None` where appropriate. Truthy checks handle None naturally.

---

## 2. SELF Placeholder String ✅ FIXED

**Pattern**: Using literal string `'SELF'` for self-signed network events.

| Location | Code | Status |
|----------|------|--------|
| ~~`network.py:48`~~ | ~~`'signed_by': 'SELF'`~~ | ✅ Network events now omit `signed_by` field entirely |
| ~~`network.py:98-99`~~ | ~~Verifies `signed_by == 'SELF'`~~ | ✅ Now verifies using `network_pubkey` from event body |
| ~~`recorded.py:185`~~ | ~~Skips dependency check for `'SELF'`~~ | ✅ Removed |

**Resolution**: Network events are self-signed using their own keypair. No `signed_by` field needed - verification uses `network_pubkey` from the event body. The network is the root of trust.

---

## 3. Direct Table Inserts Bypassing Event Store

**Pattern**: Direct SQL inserts into projection tables instead of going through `store.event()` -> `recorded.project()` cascade.

| Location | Table | Notes |
|----------|-------|-------|
| ~~`user.py:342-344`~~ | ~~`peer_self`~~ | ✅ FIXED: Now handled by peer_shared.project() |
| ~~`user.py:628-630`~~ | ~~`peer_self`~~ | ✅ FIXED: Now handled by peer_shared.project() |
| `user.py:647` | `transit_prekeys_shared` | Direct insert from invite link data |
| `peer_shared.py:179` | `peer_self` | Direct `INSERT OR REPLACE INTO peer_self` (consolidated - single source) |
| `invite.py:783` | `transit_prekeys_shared` | Direct insert |

**Problem**: Bypasses event-sourcing model, can't be reprojected.

**Note**: `peer_self` inserts have been consolidated to a single location in `peer_shared.project()`. This is acceptable since `peer_self` is local-only state mapping "my peer_id → my peer_shared_id".

---

## 4. Hardcoded Timestamp Offsets

**Pattern**: Using `t_ms + N` to force event ordering instead of relying on dependency resolution.

| Location | Offsets Used |
|----------|--------------|
| `user.py:287-500` | `+10, +20, +30, +40, +50, +60, +61, +70, +80, +81, +82, +83, +90, +100, +101` |
| `peer_shared.py:384-443` | `+1, +2, +3, +4, +5` |
| `invite.py:213-372` | `+1, +2, +3, +4, +5` |

**Problem**: Fragile ordering assumptions, doesn't scale, hides implicit dependencies.

---

## 5. Direct project_ids() Calls

**Pattern**: Forcing immediate projection instead of letting the cascade happen naturally.

| Location | Notes |
|----------|-------|
| `user.py:409` | `recorded.project_ids([recorded_id], db)` for admin_grant |
| `user.py:441` | `recorded.project_ids([recorded_id], db)` for username_update |
| `user.py:460` | `recorded.project_ids([recorded_id], db)` for network_name_update |

**Problem**: Bypasses natural event cascade, tightly couples code.

---

## 6. skip_admin_check Bypass

**Pattern**: Boolean flag to skip authorization checks.

| Location | Notes |
|----------|-------|
| `user.py:506` | `skip_admin_check=True` - "Bootstrap: first user adds themselves" |
| `group_member.py:43,56,77` | `skip_admin_check` parameter definition and usage |
| `invite.py:430,438` | `skip_admin_check` for invite projections |

**Problem**: Security bypass that could be misused; should be modeled differently.

---

## 7. Empty String Placeholders for IDs ✅ FIXED

**Pattern**: Using empty strings `''` for IDs that don't exist yet.

| Location | Code | Status |
|----------|------|--------|
| ~~`user.py:306-308`~~ | ~~`group_id=''`, `channel_id=''`, `key_id=''`~~ | ✅ Now uses `None`, fields omitted from event if not provided |

**Resolution**: Changed to `None` and invite.create_bootstrap_user_invite() only includes fields if they have values.

---

## 8. Bootstrap Fallback Patterns

**Pattern**: `project()` functions falling back to `store.get()` when projection tables don't have data yet.

| Location | Pattern |
|----------|---------|
| `user.py:135-155` | "First try invites table, then fall back to store blob (bootstrap)" |
| `peer_shared.py:120-128` | "Try store blob (bootstrap case)" |
| `invite.py:484-492` | "Try store blob (bootstrap case)" |

**Problem**: Implicit dependencies not declared in `check_deps()`.

---

## 9. Implicit Dependencies (return None without blocking) - PARTIALLY FIXED

**Pattern**: `project()` functions querying tables and returning `None` if data missing, but not declaring those as dependencies in `check_deps()`.

| Location | Missing Blocking On | Status |
|----------|---------------------|--------|
| `user.py:139-160` | `invite_id` - queries `invites` table | Crypto verification (keep) |
| `admin.py:153-171` | `peer_shared`, `admin_grant` - queries multiple tables | Authorization check (keep) |
| ~~`group_member.py:225-244`~~ | ~~`group_id`, `user_id`~~ | ✅ FIXED: Removed redundant checks, check_deps() handles |
| `peer_shared.py:121-131` | `invite_id` - queries store | Crypto verification (keep) |

**Note**: Some checks are for cryptographic verification or authorization, not just existence. These should remain. The group_member.py checks were pure existence checks that check_deps() handles.

---

## 10. Self-Connection in Bootstrap - UPDATED

**Pattern**: Network creator invites themselves, creating self-referential state.

| Location | Code | Status |
|----------|------|--------|
| ~~`user.py:329`~~ | ~~`'inviter_peer_shared_id': 'PENDING'`~~ | ✅ Now uses `None` |

**Note**: The PENDING string is gone, but the conceptual pattern remains (network creator self-invites). This is actually the correct design - the bootstrap invite is signed by the network, not a peer.

---

## 11. INSERT OR IGNORE Hiding Failures

**Pattern**: Using `INSERT OR IGNORE` which silently fails on constraint violations.

Many locations throughout the codebase. Some are legitimate idempotency, but in bootstrap code they may hide ordering/dependency bugs.

**Problem**: Silent failures make debugging difficult.

---

## 12. Bare Exception Catches

**Pattern**: Catching all exceptions and continuing.

| Location | Code |
|----------|------|
| `sync.py:906` | `except Exception: pass` |
| `sync.py:928` | `except:` (bare except) |

**Problem**: Hides real errors.

---

## 13. unsafedb in Critical Paths

**Pattern**: Using `create_unsafe_db()` to bypass recorded_by scoping.

Heavy usage in `sync_connect.py`, `invite.py`, `peer_shared.py`, `user.py`.

**Problem**: Weakens the safety guarantees of the safe db wrapper.

---

## 14. NO_DEPS_TYPES Special Cases

**Pattern**: Whitelisting event types to skip dependency checking.

| Type | Why Exempt |
|------|-----------|
| `network` | Root of trust, self-signed |
| `sync_connect` | Ephemeral, auth handled in projection |
| `sync_connect_ack` | Ephemeral, auth via implicit decryption |
| `peer` | Local peer event |
| `group_key` | Local key event |
| `transit_key` | Local key event |
| `invite_accepted` | Local-only, never synced |

**Problem**: Each special case is another thing to maintain.

---

## 15. SIGNER_ONLY_TYPES Special Cases

**Pattern**: Only checking `signed_by` dependency for certain types.

```python
SIGNER_ONLY_TYPES = {'invite', 'message_deletion', 'group_key_shared'}
```

**Problem**: More special cases.

---

## 16. Timestamp Spacing for Key Rotation

**Pattern**: Using large timestamp offsets (+1000) to "space out" key operations.

| Location | Code |
|----------|------|
| `peer_removed.py:188` | `share_timestamp = t_ms + 1000  # Space out key creation from removal events` |
| `peer_removed.py:201` | `share_timestamp += 100  # Space out timestamps for multiple groups` |
| `user_removed.py:241` | `share_timestamp = t_ms + 1000  # Space out key creation from removal events` |
| `user_removed.py:254` | `share_timestamp += 100  # Space out timestamps for multiple groups` |
| `peer_shared.py:229` | `key_share_ts = recorded_at + 1000  # Space out timestamps` |
| `channel.py:146` | `member_timestamp = t_ms + 10  # Space out timestamps to avoid collisions` |

**Problem**: Arbitrary spacing hides real ordering requirements.

---

## 17. is_foreign_local_dep() Complexity

**Pattern**: Complex function to determine when to skip dependency checks for "foreign local" dependencies.

```python
LOCAL_CREATOR_TYPES = {'transit_key', 'group_key', 'transit_prekey', 'group_prekey'}

if event_type == 'peer_shared' and field == 'peer_id': ...
if event_type == 'sync' and field == 'peer_id': return True
if event_type == 'sync_connect' and field == 'peer_id': return True
if event_type == 'transit_prekey_shared' and field == 'transit_prekey_id': return True
if event_type == 'group_prekey_shared' and field == 'group_prekey_id': return True
```

**Problem**: More per-type special-casing that's easy to get wrong.

---

## 18. Fallback to store.get() in Projection

**Pattern**: Projection functions that try projection table first, then fall back to raw blob store.

| Location | Comment |
|----------|---------|
| `user.py:148` | "This handles bootstrap case where invite_accepted hasn't unblocked invite projection yet" |
| `peer_shared.py:120` | "Try store blob (bootstrap case)" |
| `invite.py:484` | "Try store blob (bootstrap case)" |

**Problem**: Working around the fact that dependencies aren't modeled correctly.

---

## 19. KeyNotAvailableError Swallowing

**Pattern**: Catching `KeyNotAvailableError` and silently continuing.

| Location | Code |
|----------|------|
| `user.py:443` | `except username_update.KeyNotAvailableError:` |
| `user.py:462` | `except network_name_update.KeyNotAvailableError:` |
| `user.py:562` | `except username_update.KeyNotAvailableError:` |
| `user.py:750` | `except username_update.KeyNotAvailableError:` |
| `peer_shared.py:433` | `except peer_name_update.KeyNotAvailableError:` |

**Problem**: Silently failing to create name updates; user may see blank names.

---

## 20. create_for_invite() Pattern

**Pattern**: Special `create_for_invite()` function that differs from normal `create()`.

| Location | Function |
|----------|----------|
| `group_key_shared.py:78` | `create_for_invite()` - seals to invite prekey instead of peer prekey |

**Problem**: Two code paths for the same conceptual operation.

---

## 21. return_dupes Flag Confusion

**Pattern**: `return_dupes` parameter that changes store behavior.

| Values | Behavior |
|--------|----------|
| `return_dupes=True` | Return existing ID if blob already stored |
| `return_dupes=False` | Create new recorded event even for dupes |

**Problem**: Non-obvious semantics, easy to use incorrectly.

---

## 22. Hardcoded TTL Values

**Pattern**: Magic numbers for connection TTLs scattered throughout code.

| Location | Value |
|----------|-------|
| `sync_connect.py:207` | `'ttl_ms': 300000  # 5 minutes` |
| `sync_connect.py:272` | `ttl_ms = 300000` |
| `sync_connect.py:389` | `ttl_ms = 300000` |
| `sync_connect.py:455` | `'ttl_ms': 300000  # 5 minutes` |

**Problem**: Should be configurable constants, not magic numbers.

---

## 23. Triple Authentication Fallback in sync_connect

**Pattern**: Three different auth methods tried in sequence.

```python
# 1. Try invite signature first (for joiners)
if invite_id and invite_signature_b64: ...

# 2. Try peer_shared signature (for existing members we already know)
if not authenticated: ...

# 3. Try existing connection (implicit auth via decryption)
if not authenticated: ...
```

**Problem**: Complex multi-path authentication that's hard to reason about.

---

## 24. Duplicate Query for invite_id in sync_connect

**Pattern**: Same query repeated twice.

| Location | Code |
|----------|------|
| `sync_connect.py:66-70` | Query `invite_accepteds` for `invite_id` |
| `sync_connect.py:143-147` | Same query again later |

**Problem**: Redundant database access.

---

## Root Causes

1. **Circular dependencies in bootstrap** - peer_shared needs user, user needs invite, invite needs network, but we need to create all at once
2. **Event system not designed for genesis** - first events can't have valid dependencies
3. **Workarounds accumulated** - each hack enables another hack
4. **No clean separation** - bootstrap mixes direct SQL with event-sourced patterns
5. **Implicit vs explicit dependencies** - `check_deps()` doesn't capture all real dependencies

---

## Priority Fixes

1. **PENDING cleanup** - Most pervasive; pollutes entire codebase
2. **Implicit dependency audit** - Ensure `check_deps()` captures all real dependencies
3. **Direct insert audit** - Replace with proper event flow or document why exceptional
4. **Timestamp offset removal** - Replace with explicit dependencies

---

## Summary Statistics

- **21+ distinct hack patterns** identified
- **PENDING**: 6 locations
- **Direct table inserts**: 5 locations
- **Timestamp offsets**: 30+ occurrences
- **Special case types**: 7 in NO_DEPS_TYPES, 3 in SIGNER_ONLY_TYPES
- **Fallback patterns**: 3 locations
- **Silent exception catches**: 5 locations

---

*Last updated: December 2025*
