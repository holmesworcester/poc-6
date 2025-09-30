# Quiet Protocol Demo v2

A refactored demo with **command-returns-state** pattern for reliability and testability.

## Quick Start

```bash
# Run in TUI mode (default, requires textual)
pip install textual
python protocols/quiet/demo/demo_v2.py

# Run in CLI mode
python protocols/quiet/demo/demo_v2.py --cli

# Run with commands (automatically uses CLI mode)
python protocols/quiet/demo/demo_v2.py --commands "/create alice" "/network test"

# Reset database
python protocols/quiet/demo/demo_v2.py --reset-db
```

## Key Features

### Command-Returns-State Pattern

Every command returns the complete relevant state immediately:

```
> /create alice
✓ Created identity: alice
Identity: alice

> /network test
✓ Created network: test
Identity: alice
Network: test
Channel: #general
Groups (1):
  • public [id]
    # general [id]
Users (1):
  • alice
```

**No timing issues!** The general channel appears immediately because the command returns all the state.

### Interactive CLI Commands

- `/create <name>` - Create new identity
- `/network <name>` - Create network with default group and channel
- `/invite` - Generate invite code
- `/join <code> [name]` - Join network with invite
- `/group <name>` - Create new group with #general channel
- `/channel <name>` - Join existing channel
- `/channel <name> in <group>` - Create channel in specific group
- `/refresh` - Refresh current state
- `/switch <panel>` - Switch to panel (1-4)
- `/help` - Show help
- Any text without `/` sends a message to current channel

### Panel System

The demo has 4 independent panels (like 4 different users):

```bash
# In CLI mode, switch between panels
> /switch 1
> /create alice
> /network alice-net

> /switch 2
> /create bob
> /network bob-net

# Each panel has its own identity and network
```

### TUI Mode (Optional)

The TUI has 4 panels:
- **Panel 1**: Top left (visual)
- **Panel 2**: Top right (visual)
- **Panel 3**: Bottom left (visual)
- **Panel 4**: Bottom right (embedded CLI with selectable text)

The embedded CLI in Panel 4 allows testing commands while seeing visual updates in other panels.

## Architecture Benefits

1. **No Timing Issues**: Commands return state immediately
2. **Unified Logic**: CLI and TUI use identical code paths
3. **Testable**: Clear command → result pattern
4. **LLM-Friendly**: Simple, predictable interface
5. **Debugging**: Embedded CLI in TUI for testing

## Example Workflow

```bash
# Terminal 1 - Alice creates a network
python protocols/quiet/demo/demo_v2.py --reset-db
> /create alice
> /network shared-space
> /channel dev in public
> /invite
✓ Invite code: quiet://invite/...

# Terminal 2 - Bob joins the network
python protocols/quiet/demo/demo_v2.py
> /switch 2
> /join quiet://invite/... bob
> /channel dev
> Hello from Bob!

# Back in Terminal 1 - Alice sees Bob's message
> /refresh
✓ State refreshed
Users (2):
  • alice
  • bob
Recent messages:
  [bob]: Hello from Bob!
```

## Testing

Run the test suite:

```bash
python protocols/quiet/demo/test_demo_v2.py
```

Tests verify:
- Commands return complete state
- Panel isolation
- Invite/join flow
- Message flow
- Error handling

## Comparison with Original Demo

| Feature | Original Demo | Demo v2 |
|---------|--------------|---------|
| Channel appears after network creation | Sometimes (timing issue) | Always (returned in state) |
| CLI/TUI consistency | Different code paths | Same code path |
| State updates | Deferred with timers | Immediate from commands |
| Testing | Difficult (timing issues) | Simple (command→result) |
| Debugging | Hard to reproduce | Embedded CLI in TUI |