# Event Trace - Demo Implementation

This branch demonstrates a simple approach to getting human-readable event logging from actual code execution, rather than hand-written descriptions in the CLI that can drift from reality.

## The Problem

The CLI had an `EventLog` class with manual `.log()` calls:

```python
# cli.py - hand-written, not connected to backend
session.event_log.log("message", channel="<#general>", author="<alice>", content="hello")
```

If the backend changes what events it creates, the CLI logging doesn't reflect that. You can't trust it for debugging.

## The Solution

A minimal `event_trace` module (~60 lines) that event modules call directly:

```python
# In message.create() - traces what actually happened
trace('message', channel_id=channel_id[:12], author_id=user_id[:12], content=f'"{content[:30]}"')
```

The CLI starts a trace before commands and displays collected events after:

```
> send hello world
☰ event trace:
  -> message {channel_id=DrBtJnI5, author_id=iZ7Yojyo, content="hello world"}
```

## What's Implemented

**Core module:** `event_trace.py`
- `start()` - begin collecting events
- `trace(event_type, **fields)` - record an event (no-op if not started)
- `collect()` - get and clear collected events
- `render(events)` - format as human-readable strings

**Modules with trace calls:**
- `message.create()` - channel, author, content
- `channel.create()` - name, visibility, group
- `invite.create()` - mode, inviter
- `message_deletion.create()` - message_id
- `message_update.create()` - message_id, new_content
- `message_reaction.create()` - message, emoji, reactor
- `user_removed.create()` - user_id, name
- `channel_update.create()` - channel_id + updates

## Further Work

This is a demo. To make it production-ready:

1. **Add traces to more modules** - `user.new_network()`, `user.join()`, `peer_shared.create()`, group operations, key sharing, etc. Currently only ~8 modules have traces.

2. **Richer context** - Currently uses truncated IDs. Could add optional name lookups or let the CLI enrich traces with human-readable names post-collection.

3. **Structured output** - The current render is simple text. Could add JSON output for programmatic consumption, or integrate with actual logging frameworks.

4. **Trace at projection time** - Currently traces at `create()` time. Could also trace during `project()` to see what events were accepted/rejected during sync.

5. **Performance** - The contextvars approach has minimal overhead, but for high-throughput scenarios might want compile-time elimination when disabled.

## Usage

```python
# In tests or CLI
import event_trace

event_trace.start()
# ... run operations ...
events = event_trace.collect()

for e in events:
    print(f"{e['type']}: {e}")
```

Or use the built-in renderer:
```python
for line in event_trace.render(events):
    print(line)
```

In the CLI, toggle with `log on` / `log off`.
