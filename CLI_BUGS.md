# CLI Prototype Bugs

Bugs discovered through manual testing of the CLI prototype.

## Bug 1: ✅ FIXED - Joining peers have `network_???`

**Symptom:** After `new-peer --name bob --device desktop --invite 1`, Bob's account shows `network_???` instead of the actual network ID.

**Observed:**
```
ACCOUNTS:
  1. * alice (desktop) - user_0TFN2k, peer_CFkv8z, network_gX5SuL
  2.   bob (desktop) - user_jkb95o, peer_kEkLqZ, network_???
```

**Expected:** Bob should show the same network as Alice.

**Root cause:** `user.join()` doesn't return `network_id` in its result dict, or the CLI doesn't extract it properly.

**Location:**
- `cli.py:504` - `account.network_id = result.get('network_id')  # May not be available immediately`
- `events/identity/user.py` - `join()` function return value

**Impact:** Medium - Causes bugs #2 and #3

---

## Bug 2: ✅ FIXED - Joining peers can't create invites

**Symptom:** When Bob (a joiner) tries to `create-invite`, it fails with "no network joined". <!-- bob should only be able to create invites if he is admin. Bob should be able to add devices though. -->

**Observed:**
```
> create-invite
✗ no network joined
```

**Expected:** Bob should be able to create invites for his network.

**Root cause:** Consequence of Bug #1 - `account.network_id` is None for joiners.

**Location:** `cli.py:436-441` - `cmd_create_invite()` checks `account.network_id`

**Impact:** High - Joiners can't invite others

---

## Bug 3: ✅ FIXED - Joining peers don't see users in sidebar

**Symptom:** Alice sees the users list in her sidebar, but Bob and Charlie don't.

**Observed:**
```
SIDEBAR (alice (desktop)):
    1. alice
    2. bob
    3. charlie
    1. * #general

SIDEBAR (bob (desktop)):
    1. * #general
```

**Expected:** Bob and Charlie should also see the users list.

**Root cause:** Consequence of Bug #1 - `display_sidebar()` needs `account.network_id` to find the all_users group.

**Location:** `cli.py:209-222` - User display logic checks `account.network_id`

**Impact:** Medium - Poor UX for joiners

---

## Bug 4: ✅ FIXED - Excessive debug logging

**Symptom:** Running CLI commands produces thousands of lines of debug output like:
```
[STORE_BLOB] NEW blob stored: id=9p2YFaydt5ETDdBQvHNk..., size=158B
queues.blocked.notify_event_valid() no events waiting for event_id=...
```

**Expected:** Clean output showing only CLI feedback.

**Root cause:** Logging configured at DEBUG level across multiple modules.

**Location:** Various modules using `log.info()` and `log.warning()`

**Impact:** Low - Usability issue, makes CLI hard to use

---

## Bug 5: ✅ FIXED - Error hint references non-existent command

**Symptom:** When a non-admin tries to `create-invite`, the error hint suggests using `link-device`, but that command doesn't exist.

**Observed:**
```
> create-invite
✗ only admins can create invites
  hint: use 'link-device' to add another device to your account
```

**Expected:** Don't suggest commands that don't exist.

**Root cause:** The hint was added in anticipation of the device linking feature, but the command was never implemented.

**Location:** `cli.py:483` - Error handler in `cmd_create_invite()`

**Impact:** Low - Confusing UX, suggests feature that doesn't exist

---

## Bug 6: ✅ FIXED - Names with spaces don't work

**Symptom:** `new-network --name "Alice Smith" --device "MacBook Pro"` fails with argument parsing error.

**Observed:**
```
> new-network --name "Alice Smith" --device "MacBook Pro"
usage: new-network --name <name> --device <device>
cli.py: error: unrecognized arguments: Smith" Pro"
```

**Expected:** Quoted arguments should be parsed correctly.

**Root cause:** `line.split()` doesn't respect quotes - splits on all whitespace.

**Location:** `cli.py:644` - Command parsing

**Fix:** Use `shlex.split(line)` which respects shell quoting rules.

**Impact:** Medium - Can't use realistic names

---

## Bug 7: ✅ FIXED - Tracebacks shown for all errors

**Symptom:** Unhandled exceptions print full Python tracebacks even in quiet mode.

**Observed:**
```
> create-invite
error: no account selected
Traceback (most recent call last):
  File "/home/hwilson/poc-6/cli.py", line 714, in execute_command
  ...
```

**Expected:** Clean error messages without tracebacks (unless in verbose mode).

**Root cause:** Exception handler always printed traceback.

**Location:** `cli.py:775-778` - Exception handler in `execute_command()`

**Fix:** Only print traceback when `_verbose` is True.

**Impact:** Low - Noisy output for users

---

## Bug 8: ✅ FIXED - create-channel shows internal user ID in error

**Symptom:** When non-admin tries to create a channel, error shows raw user ID.

**Observed:**
```
> create-channel random
error: User JguadnT/AXKO1K+OQNxy8Q== not authorized to create channels (only admins can)
```

**Expected:** Clean error message without internal IDs.

**Root cause:** ValueError from backend not caught and translated.

**Location:** `cli.py:447-455` - `cmd_create_channel()`

**Fix:** Catch ValueError and show "✗ only admins can create channels"

**Impact:** Low - Confusing error message

---

## Suggested Fixes

### Fix for Bugs 1, 2, 3 (network_id not available) 

**Option A:** Have `user.join()` return network_id by querying after join:
```python
# In user.join(), after joining:
network_info = network.get_for_peer(peer_id, peer_id, db)
return {
    ...
    'network_id': network_info['network_id'] if network_info else None,
}
```

**Option B:** Have CLI query network_id lazily when needed:
```python
# In cmd_new_peer(), after join:
if not account.network_id:
    network_info = network.get_for_peer(account.peer_id, account.peer_id, session.db)
    if network_info:
        account.network_id = network_info['network_id']
```

### Fix for Bug 4 (debug logging)

Add logging configuration at CLI startup:
```python
import logging
logging.getLogger().setLevel(logging.WARNING)  # Suppress INFO and DEBUG
```

---

## Suggested Tests

### Test 1: Joiner has network_id
```python
def test_joiner_has_network_id():
    """After user.join(), the result should include network_id."""
    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_url, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)

    # Bob joins
    bob_peer_id = peer.create(t_ms=3000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_url, name='Bob', t_ms=4000, db=db)

    # Sync
    sync_until_converged(db, 5000)

    # Assert network_id is returned and matches Alice's
    assert 'network_id' in bob, "join() should return network_id"
    assert bob['network_id'] == alice['network_id'], "Bob should be in same network as Alice"
```

### Test 2: Joiner can see users
```python
def test_joiner_can_list_users():
    """Joiners should be able to list users in the network."""
    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    invite_id, invite_url, _ = invite.create(peer_id=alice['peer_id'], t_ms=2000, db=db)
    bob_peer_id = peer.create(t_ms=3000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_url, name='Bob', t_ms=4000, db=db)

    # Sync
    sync_until_converged(db, 5000)

    # Bob queries users
    network_info = network.get_for_peer(bob['peer_id'], bob['peer_id'], db)
    assert network_info is not None, "Bob should see the network"

    all_users_group_id = network.get_all_users_group_id(network_info['network_id'], bob['peer_id'], db)
    members = group_member.list_members(all_users_group_id, bob['peer_id'], db)

    names = [m['name'] for m in members]
    assert 'Alice' in names, "Bob should see Alice"
    assert 'Bob' in names, "Bob should see himself"
```

### Test 3: Three-player messaging via CLI
```bash
# tests/cli_tests/test_three_player.sh
new-network --name alice --device desktop
create-invite
create-invite
new-peer --name bob --device desktop --invite 1
new-peer --name charlie --device desktop --invite 2
sync --ticks 200
switch 1
send "Hello from Alice"
switch 2
send "Hello from Bob"
switch 3
send "Hello from Charlie"
sync --ticks 200
# Assertions would check message delivery
```
