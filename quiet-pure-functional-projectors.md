# Pure Functional Projectors

## Core Idea

Projectors become pure functions that:
1. Receive a **plaintext dict** containing the event and all its resolved dependencies
2. Return a **dict** describing rows to insert
3. Declare upfront which **tables they can modify**

No database lookups. No side effects. Just data in, data out.

## The Input: Dependency-Resolved Event Dict

When a projector runs, it receives everything it needs:

```python
{
    "event": {
        "event_id": "evt_123",
        "event_type": "message_sent",
        "payload": {"channel_id": "ch_1", "content": "hello", ...}
    },
    "dependencies": {
        "channel_created:ch_1": {
            "event_id": "evt_100",
            "payload": {"name": "general", "group_id": "grp_1", ...}
        },
        "group_created:grp_1": {
            "event_id": "evt_50",
            "payload": {"name": "My Group", ...}
        }
    }
}
```

Key properties:
- **Comes straight from event source**, not from projections
- **Blocked until all dependencies are valid** - the projector never sees incomplete data
- **Validation already happened** upstream (or happens in the projector itself)

## The Output: Dict of Table Rows

Projectors return a dict mapping table names to rows:

```python
def project_message(input_dict: dict) -> dict:
    event = input_dict["event"]
    channel = input_dict["dependencies"]["channel_created:" + event["payload"]["channel_id"]]

    return {
        "messages": [
            {
                "message_id": event["payload"]["message_id"],
                "channel_id": event["payload"]["channel_id"],
                "content": event["payload"]["content"],
                "group_id": channel["payload"]["group_id"]
            }
        ]
    }
```

The framework converts this to:
```sql
INSERT OR IGNORE INTO messages (message_id, channel_id, content, group_id)
VALUES (?, ?, ?, ?)
```

## Table Declaration

Each projector declares which tables it can touch:

```python
@projector(
    event_type="message_sent",
    tables=["messages", "message_counts"]
)
def project_message(input_dict: dict) -> dict:
    ...
```

Benefits:
- **Static analysis** - we know exactly what each projector writes
- **Parallel safety** - projectors touching different tables can run in parallel
- **Enforcement** - runtime error if output includes undeclared tables

## Validation in the Projector

The projector can validate and return different outputs:

```python
def project_message(input_dict: dict) -> dict:
    event = input_dict["event"]

    # Validate
    if len(event["payload"]["content"]) > 10000:
        return {"_validation": {"valid": False, "reason": "content too long"}}

    # Project
    return {
        "messages": [...],
        "_validation": {"valid": True}
    }
```

Or validation could be a separate pure function that runs first.

## Batch Processing

Since projectors are pure functions, we can batch easily:

```python
def project_batch(projector_fn, event_dicts: list[dict]) -> list[dict]:
    """Run projector on many events, collect all outputs."""
    outputs = []
    for event_dict in event_dicts:
        outputs.append(projector_fn(event_dict))
    return outputs

def apply_batch(outputs: list[dict], tables: list[str]):
    """Convert all outputs to SQL and execute in one transaction."""
    with db.transaction():
        for output in outputs:
            for table in tables:
                if table in output:
                    for row in output[table]:
                        db.execute(f"INSERT OR IGNORE INTO {table} ...")
```

We could even parallelize the pure function calls across threads/processes.

## Dependency Resolution Layer

A new layer sits between event source and projectors:

```
Event Source
     |
     v
Dependency Resolver  <-- knows which deps each event type needs
     |                   fetches them from event source
     |                   blocks until all deps are valid
     v
Projector (pure fn)
     |
     v
SQL Writer
```

The resolver:
1. Looks up the event's declared dependencies
2. Fetches those events from the event source (not projections!)
3. Checks they're all valid/unblocked
4. Assembles the input dict
5. Calls the projector

## Create Commands Still Query Projections

Commands that create events can still query projected data:

```python
def create_reply(channel_id: str, content: str) -> Event:
    # Query projection to validate channel exists
    channel = db.query("SELECT * FROM channels WHERE channel_id = ?", channel_id)
    if not channel:
        raise ValueError("Channel not found")

    # Create event (dependencies handled by event system)
    return Event(
        type="message_sent",
        payload={"channel_id": channel_id, "content": content},
        dependencies=["channel_created:" + channel_id]
    )
```

This maintains the nice DX of chaining commands based on returned event IDs.

## What This Gives Us

### Guarantees
- **No stale reads**: projector only sees data from its declared dependencies
- **No circular deps**: can't read from projections you're writing to
- **Deterministic**: same input dict = same output (testable!)
- **Correct ordering**: blocked until deps are valid, so always consistent

### Performance
- **Batch friendly**: pure functions can be batched/parallelized
- **No N+1 queries**: all data pre-fetched in the input dict
- **Predictable table writes**: can optimize write batching

### Simplicity
- **No ORM**: just dicts in, dicts out
- **No hidden state**: everything explicit in the input
- **Easy testing**: just call the function with a dict

## Resolved Questions

### 1. "All messages in channel" - Not Needed
This was a confusion. Projectors operate on **single events**, not collections. If you need "all messages in channel", that's a **query** on projected data, not a projector concern.

### 2. Updates vs Inserts
Prefer INSERT-only projections - they're idempotent and eventually consistent. For the rare cases needing UPDATE (like `channel_update` changing `name`), use GC patterns or separate tables.

**Tables that currently UPDATE in place:**
| Table | Update Use Case | Pure Alternative |
|-------|-----------------|------------------|
| `store` | message_rekey | GC + new row |
| `messages` | key_id update | Immutable, separate key_tracking table |
| `file_sync_wanted` | status changes | New status rows, query latest |
| `message_attachments` | consolidated_blob | Accumulator pattern |
| `pending_intros` | processed flag | Separate processed_intros table |
| `channels` | name, disappearing_time | channel_updates table (already exists!) |
| `networks` | creator_user_id | Should be immutable, set at creation |
| `peer_self` | user_id | Should be immutable after link |
| `groups` | key_id | key_rotation events, query latest |

### 3. Transitive Dependencies - Assume Transitive Validity
**We don't need to fetch transitive deps.** If B depends on A, and C depends on B, then C only needs to verify B is valid. B being valid implies A is valid (otherwise B couldn't be valid).

The current code was checked - no projectors actually need data from transitive dependencies. They only need:
- Immediate dependency existence (for blocking)
- Data from immediate dependencies' event payloads

### 4. Schema Evolution
Still open - but less critical than originally thought since projections are derivable from events.

## Critical Discovery: Mutable State Was a Design Bug

Initially we thought `message.project()` needed to query the `channels` table for `disappearing_time_ms` because channel settings can be updated. This seemed like an exception to pure projection.

**But this was a design bug, not a fundamental limitation!**

The fix: store `disappearing_time_ms` in the message event at creation time. Then:
- `message.create()` captures the TTL from channel settings when creating the event
- `message.project()` uses the value from `event_data['disappearing_time_ms']`
- No projection table lookup needed!

### Lesson Learned

When you think you need to query a projection table in a projector, ask:
1. **Should this data be in the event?** If the value was known at creation time, store it.
2. **Is this truly mutable state?** Or is it just not being captured correctly?

### Known Exceptions

After fixing the TTL bug, we have **no known exceptions** where projection queries are required.

| Projector | Dependency | Status |
|-----------|------------|--------|
| `message` | `channel` | FIXED - `disappearing_time_ms` now in event |
| ??? | ??? | (hunting for more...) |

## Open Questions

1. **Schema evolution?**
   - When projector output format changes, do we re-run all events?
   - Versioned projectors?

## Proof of Concept Implementation

Three complex projectors were implemented as pure functions to validate the design:

### 1. `pure_projectors/message.py`
Tests: TTL calculation from channel dependency, deletion checking
```python
@projector(event_type="message", tables=["messages", "event_dependencies"])
def project(input_dict: dict) -> ProjectorResult:
    # Gets channel's disappearing_time_ms from dependencies
    # Calculates TTL, builds output dict
    # No DB queries needed!
```

### 2. `pure_projectors/channel.py`
Tests: Admin authorization chain
```python
def project(input_dict: dict) -> ProjectorResult:
    # Verifies admin_grant authorizes the signer
    # Uses INSERT OR REPLACE (special case)
```

### 3. `pure_projectors/group_member.py`
Tests: Multiple existence checks, authorization
```python
@projector(event_type="group_member", tables=["group_members"])
def project(input_dict: dict) -> ProjectorResult:
    # Checks group exists, user exists
    # Verifies admin_grant chain or legacy creator auth
```

### Resolver Layer
`pure_projectors/resolver.py` builds input dicts from event store:
- Fetches event blob, unwraps, verifies signature
- Resolves immediate dependencies
- Pre-computes validation results (e.g., deletion validity)

### Test Results
All tests pass, proving pure projectors produce identical output to current projectors:
```
test_pure_message_projector - SUCCESS
test_pure_channel_projector - SUCCESS
test_pure_group_member_projector - SUCCESS
test_message_projector_with_disappearing_messages - SUCCESS
```

## Comparison to Current System

| Current | Pure Functional |
|---------|-----------------|
| Projectors query DB | Projectors receive pre-fetched dict |
| Side effects in projector | Pure function returns dict |
| Implicit table access | Declared table list |
| Individual event processing | Natural batching |
| Validation separate | Validation in projector (or separate pure fn) |

## Example: Full Flow

```python
# 1. Projector definition
@projector(
    event_type="membership_granted",
    tables=["memberships"],
    dependencies=["user_created:{payload.user_id}", "group_created:{payload.group_id}"]
)
def project_membership(input: dict) -> dict:
    event = input["event"]
    user = input["dependencies"][f"user_created:{event['payload']['user_id']}"]
    group = input["dependencies"][f"group_created:{event['payload']['group_id']}"]

    return {
        "memberships": [{
            "membership_id": event["payload"]["membership_id"],
            "user_id": event["payload"]["user_id"],
            "group_id": event["payload"]["group_id"],
            "user_name": user["payload"]["name"],  # denormalized
            "group_name": group["payload"]["name"]  # denormalized
        }]
    }

# 2. Framework resolves dependencies and calls projector
input_dict = resolver.resolve(event)  # blocks until deps valid
output = project_membership(input_dict)

# 3. Framework applies output
writer.apply(output, tables=["memberships"])
# -> INSERT OR IGNORE INTO memberships (membership_id, ...) VALUES (...)

# 4. Commands can query the projection
def list_memberships(user_id: str):
    return db.query("SELECT * FROM memberships WHERE user_id = ?", user_id)
```
