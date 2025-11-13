# CLI Architecture Rules

## CRITICAL RULE: Function-Only API Access

### ⚠️ THIS RULE MUST NEVER BE BROKEN ⚠️

The CLI implementation must:

1. **Use ONLY event functions from `events/` modules**
   - ✅ CORRECT: `user.new_network(name='Alice', t_ms=1000, db=db)`
   - ✅ CORRECT: `message.create(peer_id=..., channel_id=..., content='Hello', t_ms=2000, db=db)`
   - ✅ CORRECT: `message.list_messages(channel_id, peer_id, db)`
   - ❌ WRONG: `db.query("SELECT * FROM messages WHERE ...")`
   - ❌ WRONG: Direct access to any projection tables

2. **NEVER access the database directly**
   - No raw SQL queries in CLI code
   - No direct table reads or writes
   - All database interaction through event functions ONLY

3. **Follow the same patterns as scenario tests**
   - Look at `tests/scenario_tests/` for examples
   - If a scenario test can do it, the CLI can do it
   - If a scenario test doesn't do it, neither should the CLI

4. **Think of this as an API client**
   - The event functions ARE the API
   - The CLI is a client that consumes this API
   - Same constraints as a real API client (no direct DB access)

## Why This Rule Exists

### 1. Maintainability
- All business logic stays in event functions
- Changes to schema/projections don't break CLI
- CLI remains simple and focused on user interaction

### 2. Consistency
- CLI behavior matches scenario tests exactly
- Both use the same API surface
- Easier to reason about system behavior

### 3. API Readiness
- When we build a real API, the CLI will already be using it correctly
- Functions in `events/` modules become the API endpoints
- No refactoring needed to transition from in-memory to networked

### 4. Testing
- CLI tests validate the same functions as scenario tests
- Bugs found in CLI usage inform API design
- User-facing workflows help identify missing functions

## Allowed Database Operations

The CLI may:
- ✅ Create and initialize database: `db = Database(conn)`, `schema.create_all(db)`
- ✅ Commit transactions: `db.commit()`
- ✅ Pass `db` parameter to event functions
- ✅ Use event functions that return query results

The CLI must NOT:
- ❌ Execute raw SQL queries: `db.query("SELECT ...")`
- ❌ Read from projection tables directly
- ❌ Write to tables directly (except through event functions)
- ❌ Use `create_safe_db()` or `create_unsafe_db()` (internal to event functions)

## State Display Functions

When displaying state, the CLI must use these event module functions:

### From `events.content.channel`
```python
channel.list_channels(peer_id, db) -> list[dict]
```

### From `events.content.message`
```python
message.list_messages(channel_id, peer_id, db) -> list[dict]
```

### From `events.group.group`
```python
group.list_all_groups(peer_id, db) -> list[dict]
```

### From `events.group.group_member`
```python
group_member.list_members(group_id, peer_id, db) -> list[dict]
group_member.is_member(user_id, group_id, peer_id, db) -> bool
```

### From `events.identity.network`
```python
network.get_all_users_group_id(network_id, peer_id, db) -> str
network.get_admin_group_id(network_id, peer_id, db) -> str
```

### From `events.identity.invite`
```python
# If this function exists:
invite.list_invites(peer_id, db) -> list[dict]

# If not, the CLI should request this function be added
```

## What to Do When a Function Doesn't Exist

If the CLI needs to display information that isn't available through event functions:

1. **DO NOT** query the database directly as a workaround
2. **DO** identify the missing function needed
3. **DO** create an issue/task to add the function to the appropriate event module
4. **DO** implement the function in the event module following existing patterns
5. **THEN** use the new function in the CLI

### Example: Missing Function Flow

**Scenario:** CLI needs to list all invites for a peer.

**Wrong approach:**
```python
# ❌ NEVER DO THIS
invites = db.query_all(
    "SELECT * FROM invites WHERE recorded_by = ?",
    (peer_id,)
)
```

**Correct approach:**
```python
# 1. Check if function exists in events.identity.invite module
# 2. If not, add it:

# In events/identity/invite.py:
def list_invites(peer_id, db):
    """List all invites visible to a peer."""
    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=peer_id)
    return safedb.query_all(
        "SELECT * FROM invites WHERE recorded_by = ? ORDER BY created_at DESC",
        (peer_id,)
    )

# 3. Then use it in CLI:
from events.identity import invite

invites = invite.list_invites(peer_id, db)
```

## Code Review Checklist

Before committing any CLI code, verify:

- [ ] No raw SQL queries in CLI files
- [ ] All data access goes through `events/` module functions
- [ ] All new data access needs are addressed by adding functions to `events/` modules
- [ ] CLI patterns match scenario test patterns
- [ ] All database operations are either:
  - Setup: `schema.create_all()`, `db.commit()`
  - Event functions: `user.new_network()`, `message.create()`, etc.
  - Query functions: `message.list_messages()`, `group.list_all_groups()`, etc.

## Function Discovery Guide

When implementing CLI commands, find the right functions here:

### Identity & Network
- `events.identity.user` - Create networks, join networks, link devices
- `events.identity.peer` - Create peers
- `events.identity.invite` - Create and accept invites
- `events.identity.link_invite` - Create device link invites
- `events.identity.link` - Accept device links
- `events.identity.network` - Query network information

### Groups & Membership
- `events.group.group` - Create groups, list groups
- `events.group.group_member` - Add members, check membership, list members

### Content
- `events.content.channel` - Create channels, list channels
- `events.content.message` - Create messages, list messages

### Network Operations
- `tick.tick()` - Run sync rounds (from `tick` module)

### Database Setup
- `schema.create_all(db)` - Initialize all tables (from `schema` module)
- `db.commit()` - Commit transactions (from `db` module)

## Summary

**The golden rule:** If you can't find a function to do what you need, ADD IT to the appropriate event module. Don't query the database directly from the CLI.

This keeps the CLI clean, the API surface well-defined, and the codebase maintainable.
