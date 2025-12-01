# Disappearing Messages CLI - Mockup & Implementation Plan

## Status

**Backend**: Already implemented
- `channel_update.create()` - Update channel's disappearing_time_ms (admin-only)
- `channel.create()` - Supports disappearing_time_ms parameter
- `message.list()` - Returns messages with ttl_ms
- `purge_expired.run_purge_expired_for_all_peers()` - Deletes expired messages
- Scenario tests in `tests/scenario_tests/test_disappearing_messages_realistic.py`

**CLI Frontend**: NOT YET IMPLEMENTED (this plan)

---

## CLI Mockup

### 1. Set Disappearing Messages (`set-disappearing`)

Uses selected channel. Requires admin. **Changes only affect subsequent messages** - existing messages keep their original expiration time (or lack thereof).

```
poc-6> select-channel 1
Selected: #general

poc-6> set-disappearing --days 7
Set disappearing messages to 7 days for #general (affects new messages only)

poc-6> set-disappearing --time 3600000
Set disappearing messages to 1 hour for #general (affects new messages only)

poc-6> set-disappearing --off
Turned off disappearing messages for #general (affects new messages only)
```

**Error - Not Admin**:
```
poc-6> set-disappearing --days 7
Error: Only admins can change disappearing messages settings
```

**Error - No Channel Selected**:
```
poc-6> set-disappearing --days 7
Error: No channel selected. Use 'select-channel <n>' first.
```

---

### 2. View Expiration Time in Messages

Messages show time remaining when disappearing is enabled:

```
poc-6> show

MAIN (#general):
  1. [1000ms] alice: Hello everyone! (expires in: 6d 23h)
  2. [2000ms] bob: Hey Alice! (expires in: 6d 23h)
  3. [500000ms] alice: Meeting at 3pm (expires in: 6d 17h)
```

When disappearing is off (ttl_ms = 0):
```
poc-6> show

MAIN (#general):
  1. [1000ms] alice: Hello everyone!
  2. [2000ms] bob: Hey Alice!
```

---

### 3. Channel List Shows Disappearing Setting

```
poc-6> list-channels

Channels:
  1.   #general (disappearing: 7d)
  2. * #random
  3.   #announcements (disappearing: 30d)
```

Format rules:
- `Xd` for days when >= 1 day
- `Xh` for hours when < 1 day but >= 1 hour
- `Xm` for minutes when < 1 hour
- Omit entirely when disappearing_time_ms = 0

---

### 4. Fast-Forward Time (`fast-forward`)

Jump simulation time forward by days (without running each tick). Runs purge and then shows state.

```
poc-6> fast-forward --days 3
Fast-forwarded 3 days (current time: 259201000ms)

STATE:
  ...

MAIN (#general):
  1. [1000ms] alice: Hello! (expires in: 4d 0h)
  ...
```

After messages expire:
```
poc-6> fast-forward --days 7

Fast-forwarded 7 days (current time: 863401000ms)

STATE:
  ...

MAIN (#general):
  (no messages)
```

---

### 5. Help Text Updates

```
poc-6> help

available commands:
  ...existing commands...
  set-disappearing --days <n> | --time <ms> | --off
  fast-forward --days <n>
  ...
```

---

## Implementation Plan

### Phase 1: Utility Functions

Add to `cli.py`:

```python
def format_duration_short(ms: int) -> str:
    """Format milliseconds as short duration: 7d, 12h, 30m"""
    if ms <= 0:
        return ""
    days = ms // (24 * 60 * 60 * 1000)
    if days >= 1:
        return f"{days}d"
    hours = ms // (60 * 60 * 1000)
    if hours >= 1:
        return f"{hours}h"
    minutes = ms // (60 * 1000)
    return f"{minutes}m"

def format_expires_in(expires_at_ms: int, current_time_ms: int) -> str:
    """Format time remaining until expiration: 'expires in: 6d 23h'"""
    remaining = expires_at_ms - current_time_ms
    if remaining <= 0:
        return "(expired)"
    days = remaining // (24 * 60 * 60 * 1000)
    hours = (remaining % (24 * 60 * 60 * 1000)) // (60 * 60 * 1000)
    if days > 0:
        return f"(expires in: {days}d {hours}h)"
    minutes = (remaining % (60 * 60 * 1000)) // (60 * 1000)
    if hours > 0:
        return f"(expires in: {hours}h {minutes}m)"
    return f"(expires in: {minutes}m)"
```

---

### Phase 2: Commands

#### 2.1 `cmd_set_disappearing()`

```python
def cmd_set_disappearing(session: CLISession, days: int | None = None, time_ms: int | None = None, off: bool = False):
    """Set disappearing messages time for selected channel."""
    if not session.selected_channel_id:
        print("Error: No channel selected. Use 'select-channel <n>' first.")
        return

    account = session.get_selected_account()

    # Check admin
    if not invite.is_admin(account.peer_shared_id, account.peer_id, session.db):
        print("Error: Only admins can change disappearing messages settings")
        return

    # Calculate TTL
    if off:
        ttl_ms = 0
    elif days:
        ttl_ms = days * 24 * 60 * 60 * 1000
    else:
        ttl_ms = time_ms

    # Get channel name for display
    channels = channel.list_channels(recorded_by=account.peer_id, db=session.db)
    channel_name = next((ch['name'] for ch in channels if ch['channel_id'] == session.selected_channel_id), '???')

    try:
        channel_update.create(
            channel_id=session.selected_channel_id,
            peer_id=account.peer_id,
            peer_shared_id=account.peer_shared_id,
            t_ms=session.current_time_ms,
            db=session.db,
            new_disappearing_time_ms=ttl_ms
        )
        session.db.commit()
        session.current_time_ms += 100

        if ttl_ms == 0:
            print(f"Turned off disappearing messages for #{channel_name} (affects new messages only)")
        else:
            print(f"Set disappearing messages to {format_duration_short(ttl_ms)} for #{channel_name} (affects new messages only)")

        session.run_auto_tick()
    except ValueError as e:
        print(f"Error: {e}")
```

#### 2.2 `cmd_fast_forward()`

```python
def cmd_fast_forward(session: CLISession, days: int):
    """Fast-forward simulation time by days."""
    ms = days * 24 * 60 * 60 * 1000
    session.current_time_ms += ms

    # Run purge to delete expired messages
    import purge_expired
    purge_expired.run_purge_expired_for_all_peers(session.current_time_ms, session.db)
    session.db.commit()

    print(f"Fast-forwarded {days} day{'s' if days != 1 else ''} (current time: {session.current_time_ms}ms)")
    print()
    display_state(session)
```

---

### Phase 3: Display Updates

#### 3.1 Update `display_main()` - Show expiration

In the message loop, after printing content:

```python
# Show expiration if message has TTL
ttl_ms = msg.get('ttl_ms', 0)
if ttl_ms > 0:
    expires_str = format_expires_in(ttl_ms, session.current_time_ms)
    # Append to message line
```

#### 3.2 Update channel list display

When listing channels, show disappearing setting:

```python
for i, ch in enumerate(channels, 1):
    selected = "*" if ch['channel_id'] == session.selected_channel_id else " "
    disappearing = ch.get('disappearing_time_ms', 0)
    if disappearing > 0:
        disappearing_str = f" (disappearing: {format_duration_short(disappearing)})"
    else:
        disappearing_str = ""
    print(f"    {i}. {selected} #{ch['name']}{disappearing_str}")
```

---

### Phase 4: Command Registration

Add to `execute_command()`:

```python
elif cmd == "set-disappearing":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--days", type=int)
    parser.add_argument("--time", type=int)
    parser.add_argument("--off", action="store_true")
    try:
        args = parser.parse_args(parts[1:])
        if not args.days and not args.time and not args.off:
            print("usage: set-disappearing --days <n> | --time <ms> | --off")
        else:
            cmd_set_disappearing(session, days=args.days, time_ms=args.time, off=args.off)
    except SystemExit:
        print("usage: set-disappearing --days <n> | --time <ms> | --off")

elif cmd == "fast-forward":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--days", type=int, required=True)
    try:
        args = parser.parse_args(parts[1:])
        cmd_fast_forward(session, args.days)
    except SystemExit:
        print("usage: fast-forward --days <n>")
```

Add to help text.

---

### Phase 5: Tests

#### 5.1 CLI Tests (`tests/cli/test_disappearing_messages_cli.py`)

```python
def test_set_disappearing_days():
    """Admin can set disappearing messages by days."""
    commands = """
new-network --name "Test" --username alice --device desktop
create-channel test-channel
select-channel 2
set-disappearing --days 7
list-channels
"""
    result = run_cli(commands)
    assert result.returncode == 0
    assert "Set disappearing messages to 7d for #test-channel (affects new messages only)" in result.stdout
    assert "(disappearing: 7d)" in result.stdout


def test_set_disappearing_time():
    """Admin can set disappearing messages by milliseconds."""
    commands = """
new-network --name "Test" --username alice --device desktop
create-channel test-channel
select-channel 2
set-disappearing --time 3600000
list-channels
"""
    result = run_cli(commands)
    assert result.returncode == 0
    assert "Set disappearing messages to 1h for #test-channel (affects new messages only)" in result.stdout
    assert "(disappearing: 1h)" in result.stdout


def test_set_disappearing_off():
    """Admin can turn off disappearing messages."""
    commands = """
new-network --name "Test" --username alice --device desktop
create-channel test-channel
select-channel 2
set-disappearing --days 7
set-disappearing --off
list-channels
"""
    result = run_cli(commands)
    assert result.returncode == 0
    assert "Turned off disappearing messages for #test-channel (affects new messages only)" in result.stdout
    # Channel should not show "(disappearing: ...)" anymore


def test_set_disappearing_non_admin_error():
    """Non-admin gets error when trying to set disappearing messages."""
    # This test needs two peers - Alice (admin) and Bob (non-admin)
    # Bob tries to set disappearing messages and gets error
    ...


def test_set_disappearing_no_channel_error():
    """Error when no channel selected."""
    commands = """
new-network --name "Test" --username alice --device desktop
set-disappearing --days 7
"""
    result = run_cli(commands)
    assert "No channel selected" in result.stdout


def test_fast_forward_deletes_expired_messages():
    """Fast-forward causes expired messages to be deleted."""
    commands = """
new-network --name "Test" --username alice --device desktop
create-channel ephemeral
select-channel 2
set-disappearing --days 1
send Hello world
show
fast-forward --days 2
show
"""
    result = run_cli(commands)
    assert result.returncode == 0
    # First show should have "Hello world"
    # After fast-forward, should have "(no messages)"


def test_message_shows_expiration_time():
    """Messages show 'expires in Xd Yh' when disappearing enabled."""
    commands = """
new-network --name "Test" --username alice --device desktop
create-channel ephemeral
select-channel 2
set-disappearing --days 7
send This will disappear
show
"""
    result = run_cli(commands)
    assert result.returncode == 0
    assert "expires in:" in result.stdout
```

---

## Files to Modify

| File | Changes |
|------|---------|
| `cli.py` | Add `format_duration_short()`, `format_expires_in()`, `cmd_set_disappearing()`, `cmd_fast_forward()`, update `display_main()`, update channel display, update `execute_command()`, update help |
| `tests/cli/test_disappearing_messages_cli.py` | **NEW** - CLI tests |

---

## Demo Script

After implementation, the demo flow would be:

```bash
# Start CLI
python cli.py

# Setup
new-network --name "Demo" --username alice --device laptop
create-channel secret-chat

# Enable disappearing messages
select-channel 2
set-disappearing --days 1

# Send some messages
send "This message will disappear in 1 day"
send "So will this one"
show

# See expiration times in message list
# Output: "[1000ms] alice: This message will disappear... (expires in: 23h 59m)"

# Fast-forward past expiration
fast-forward --days 2
show

# Messages are gone!
# Output: "(no messages)"
```

This demonstrates the complete lifecycle: configure, send, see expiration, time passes, messages deleted.
