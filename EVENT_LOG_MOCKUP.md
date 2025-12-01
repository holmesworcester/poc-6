# Event Log Feature Mockup

## Overview

This feature adds a human-readable log of events created by CLI commands. When enabled, the log displays after command output, showing what events were created and their key details.

## Design Principles

1. **Command-level logging** - Log at the command handler level, not deep in store.event(). This gives us access to human-readable context (channel names, user names, content) rather than just IDs.

2. **Explicit capture** - Commands explicitly log their events via a simple API rather than relying on intercepting store.event() calls.

3. **Lightweight** - Minimal code changes per command. A single function call to log each event.

4. **Human readable** - Show meaningful information: names, content previews, relationships.

**Note on legacy logging:** There's extensive debug logging in event modules (100+ lines with `[TAG]` format), but that's diagnostic logging for troubleshooting, not user-facing. Cleaning it up is orthogonal to this feature.

---

## User Experience

### Toggle Command

```
> toggle-log
event log: ON

> toggle-log
event log: OFF
```

---

## Display Format

### Chosen: JSON-lite with Angle Bracket Substitutions

JSON-lite format showing event data structure. Angle brackets `<>` denote human-readable substitutions for event IDs:

```
EVENT LOG:
  → message {channel=<#general>, author=<alice>, content="hello everyone!"}
  → channel {name=random, group=<all_users>}
  → user {name=alice, invite=<from bob>}
```

- Literal values (strings, numbers): unbracketed
- ID lookups (resolved to names): `<value>`

### Options Considered

| Option | Example | Why Not Chosen |
|--------|---------|----------------|
| Prose description | `→ message: sent "hello" in #general` | Less structured, harder to parse visually |
| Pipe-delimited | `→ message \| #general \| alice \| "hello"` | Field meaning unclear without labels |
| Key=value (no braces) | `→ message: channel=#general author=alice` | Less JSON-like, harder to distinguish event type from fields |
| Full JSON | `→ {"type": "message", ...}` | Too verbose, noisy |

For message operations (delete/edit/reaction), we show a **content preview** rather than ordinal numbers since ordinals are frontend-only and ephemeral.

---

## Event-Specific Display

### Message Events

```
→ message {channel=<#general>, author=<alice>, content="hello everyone!"}
```

### Channel Events

```
→ channel {name=random, group=<all_users>}
```

### Invite Events (User vs Device)

User invites allow someone new to join the network. Device invites allow an existing user to link another device.

```
→ user_invite {created_by=<alice>}
→ device_invite {created_by=<alice>, for_user=<alice>}
```

### User Events

```
→ user {name=alice, invite=<from bob>}
```

### Message Deletion/Update/Reaction

```
→ message_deletion {content=<"hello everyo...">, channel=<#general>}
→ message_update {content=<"hello everyo...">, channel=<#general>, new_content="new content..."}
→ message_reaction {content=<"hello everyo...">, channel=<#general>, emoji=👍, by=<alice>}
```

---

## Full Example: send

```
> send hello everyone!
⟳ auto-syncing 100 ticks...
✓ synced (t=1000ms -> 11000ms)

EVENT LOG:
  → message {channel=<#general>, author=<alice>, content="hello everyone!"}

ACCOUNTS:
...
```

---

## Full Example: create-channel

```
> create-channel random
⟳ auto-syncing 100 ticks...
✓ synced (t=11000ms -> 21000ms)

EVENT LOG:
  → channel {name=random, group=<all_users>}

ACCOUNTS:
...
```

---

## Full Example: new-network

```
> new-network alice desktop
⟳ auto-syncing 100 ticks...
✓ synced (t=0ms -> 10000ms)

EVENT LOG:
  → peer {name=alice, device=desktop}
  → network {signed_by=<self>}
  → user_invite {type=bootstrap}
  → user {name=alice, invite=<bootstrap>}
  → peer_shared {user=<alice>, device=desktop}
  → admin_grant {user=<alice>}
  → group {name=all_users}
  → channel {name=general, group=<all_users>}

ACCOUNTS:
...
```

---

## Full Example: new-peer (Join Network)

```
> new-peer bob phone 1
⟳ auto-syncing 100 ticks...
✓ synced (t=21000ms -> 31000ms)

EVENT LOG:
  → peer {name=bob, device=phone}
  → invite_accepted {invite=<from alice>}
  → user {name=bob, invite=<from alice>}
  → peer_shared {user=<bob>, device=phone}

ACCOUNTS:
...
```

---

## Implementation Design

### 1. EventLog Class

```python
class EventLog:
    """Captures human-readable event descriptions during command execution."""

    def __init__(self):
        self.entries: list[str] = []
        self.enabled: bool = False

    def clear(self):
        """Clear log entries before each command."""
        self.entries = []

    def log(self, event_type: str, **fields):
        """Log an event with key=value fields.

        Use angle brackets in values to denote ID substitutions:
            log("message", channel="<#general>", author="<alice>", content="hello")
        """
        if self.enabled:
            field_strs = [f"{k}={v}" for k, v in fields.items()]
            self.entries.append(f"→ {event_type} {{{', '.join(field_strs)}}}")

    def display(self):
        """Display log entries if any exist and logging is enabled."""
        if self.enabled and self.entries:
            print("EVENT LOG:")
            for entry in self.entries:
                print(f"  {entry}")
            print()
```

### 2. Add to CLISession

```python
class CLISession:
    def __init__(self):
        # ... existing fields ...
        self.event_log = EventLog()
```

### 3. Toggle Command Handler

```python
def cmd_toggle_log(session: CLISession):
    """Toggle event logging on/off."""
    session.event_log.enabled = not session.event_log.enabled
    state = "ON" if session.event_log.enabled else "OFF"
    print(f"event log: {state}")
```

### 4. Modify execute_command()

```python
def execute_command(session: CLISession, line: str, show_prompt: bool = True) -> bool:
    # Clear log before each command
    session.event_log.clear()

    # ... existing command dispatch ...

    # After auto-tick, before display_state()
    session.event_log.display()

    # ... display_state() etc ...
```

---

## Commands to Modify

| Command | Events Created |
|---------|---------------|
| `new-network` | peer, network, user_invite (bootstrap), user, peer_shared, admin_grant, group, channel |
| `new-peer` | peer, invite_accepted, user, peer_shared |
| `send` | message |
| `create-channel` | channel |
| `create-invite` | user_invite or device_invite (depending on type) |
| `delete-message` | message_deletion |
| `edit-message` | message_update |
| `add-reaction` | message_reaction |

---

## Design Decisions

1. **No timestamps or IDs** - Keep it simple and human-readable
2. **Show all events** - Commands like `new-network` show all 8 events, no summarization
3. **Per-command display** - Log clears before each command, shows only that command's events
4. **No filtering** - Keep it simple for now
5. **`toggle-log`** - Short command name
6. **User invite vs device invite** - Distinguish between inviting new members vs linking devices

