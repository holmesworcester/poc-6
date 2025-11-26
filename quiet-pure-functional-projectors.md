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

## Open Questions

1. **How do we express "all messages in channel"?** The input dict has specific events, not collections. Maybe:
   - Projections can query other projections for aggregates?
   - Or: aggregate projections are a different kind of projector with different rules?

2. **What about projections that update, not just insert?**
   - `INSERT OR IGNORE` only works for new rows
   - Could support `INSERT OR REPLACE` for idempotent updates?
   - Or explicit `UPSERT` patterns?

3. **How do we handle "derived" dependencies?**
   - Event A depends on event B, but we also need to know about event C which B depended on
   - Flatten all transitive deps? Or just immediate?

4. **Schema evolution?**
   - When projector output format changes, do we re-run all events?
   - Versioned projectors?

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
