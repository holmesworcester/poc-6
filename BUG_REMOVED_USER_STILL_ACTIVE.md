# Bug: Removed User Can Still Send Messages and Appears in User List

## Summary
When an admin removes a user via `remove-user`, the removed user's CLI account remains fully functional. They can still send messages, see channels, and appear in the user list.

## Steps to Reproduce

```bash
# In CLI:
new-network --name TestNet --username alice --devicename desktop
create-invite
join --username bob --devicename laptop --invite 1

# Alice removes Bob
switch 1
remove-user 2

# Bob can still operate!
switch 2
send "bob tries to send after removal"  # ✓ This SUCCEEDS - BUG!
list-messages  # Bob still sees messages
```

## Expected Behavior
After `remove-user 2`:
1. Bob should NOT be able to send messages
2. Bob should NOT appear in the user list for other users
3. Bob's account should be marked as removed/inactive
4. Operations from Bob's account should fail gracefully

## Actual Behavior
After `remove-user 2`:
1. Bob CAN still send messages (they appear in the channel)
2. Bob STILL appears in the user list
3. Bob's account works normally
4. No indication that Bob was removed

## Observed Output

```
> remove-user 2
✓ removed user #2: bob

> switch 2
✓ selected account #2: bob (laptop)

> send "bob tries to send after removal"
✓ sent message     <-- Should fail!

SIDEBAR (bob (laptop)):
  users:
    1. alice
    2. bob           <-- Should not appear!
    3. charlie

  channels:
    1. * #general

MAIN (#general):
  1. [81200ms] bob: bob tries to send after removal   <-- Should not exist!
```

## Analysis

### What `remove-user` Does
The `remove-user` command calls `user_removed.create()` which likely:
1. Creates a `user_removed` event
2. May update some state in the database

### What's Missing
The CLI doesn't check if the current user has been removed before:
1. Allowing `send` operations
2. Displaying the user in `list-users`
3. Showing the account as valid

### Possible Root Causes

1. **Event not propagated to CLI session state**: The CLI maintains `AccountContext` objects that don't get invalidated when a user is removed.

2. **User list query doesn't filter removed users**: The `user.list_for_display()` or similar function may not be filtering out users with `user_removed` events.

3. **Message creation doesn't check removal status**: The `message.create()` backend may not verify if the peer's user has been removed.

4. **CLI-only simulation artifact**: Since this is a single-process CLI simulation, the "removed" user's account is still in memory and connected to the same database.

### Files to Investigate

1. `events/identity/user_removed.py` - What does the removal event do?
2. `events/identity/user.py` - Does `list_for_display()` filter removed users?
3. `events/content/message.py` - Does `create()` check user status?
4. `cli.py` - How does AccountContext track validity?

### Potential Fixes

1. **Add removal check to CLI operations**: Before any operation, check if the current user has been removed.

2. **Filter removed users from list**: Update `user.list_for_display()` to exclude users with removal events.

3. **Block message creation for removed users**: Add a check in `message.create()` that rejects messages from removed users.

4. **Invalidate AccountContext on removal**: When a user_removed event is processed, mark that account as invalid.

## Related Tests to Add

```python
def test_removed_user_cannot_send():
    """Removed user should not be able to send messages."""
    # Setup: alice creates network, invites bob
    # Alice removes bob
    # Switch to bob, try to send - should fail

def test_removed_user_not_in_user_list():
    """Removed user should not appear in user list."""
    # Setup: alice creates network, invites bob
    # Alice removes bob
    # list-users should only show alice
```
