# Quiet Protocol Demo

The Quiet Protocol Demo showcases the envelope-based event processing system with a modern TUI inspired by poc-3's design.

## Features

### TUI Mode (Default)
- **4 Identity Panels**: Manage multiple identities simultaneously
- **State Inspector**: Real-time view of database state
- **Protocol Events Log**: Live feed of all protocol events
- **Control Bar**: Quick access to refresh and clear functions
- **Help Modal**: Press `?` for keyboard shortcuts and commands

### CLI Mode
- Same functionality as TUI but command-line driven
- Perfect for scripting and testing
- Supports batch commands

## Running the Demo

### Setup
```bash
cd /home/hwilson/quiet-python-poc-5
python3 -m venv venv
source venv/bin/activate
pip install requests PyYAML textual pynacl
```

### TUI Mode (Interactive)
```bash
source venv/bin/activate
python protocols/quiet/demo.py --start-server
```

### CLI Mode
```bash
# Interactive CLI
source venv/bin/activate
python protocols/quiet/demo.py --start-server --cli

# Batch commands
python protocols/quiet/demo.py --start-server --cli --commands "identity create 1" "panel 1"
```

## TUI Layout

```
+-------------------+-------------------+
| Identity 1        | Identity 2        |
| /create          | /create          |
| /transit         | /transit         |
| /group [name]    | /group [name]    |
+-------------------+-------------------+
| State Inspector   | Protocol Events   |
| • identities: 2   | 0001 ✓ Created... |
| • events: 5       | 0002 → [validate] |
| • keys: 3         | 0003 ⚡ Process... |
+-------------------+-------------------+
```

Note: The current implementation uses a 2x2 grid layout for better stability.

## Keyboard Shortcuts (TUI)

- `Tab` - Switch between panels
- `?` - Show help modal
- `Ctrl+R` - Refresh state
- `Ctrl+C` - Clear event log
- `q` - Quit application
- `Escape` - Close modals

## Panel Commands

Type these in any identity panel's input field:

- `/create` - Create new identity
- `/transit` - Create transit key (requires identity)
- `/group [name]` - Create group key (requires identity)
- `/help` - Show help in panel

## CLI Commands

Available in CLI mode:

- `identity create <panel>` - Create identity for panel (1-4)
- `identity list` - List all identities
- `transit-key create <panel>` - Create transit key
- `group-key create <panel> <group>` - Create group key
- `panel <id>` - Show panel state
- `db` - Show database state
- `events [limit]` - Show recent events
- `refresh` - Refresh state from API
- `help` - Show available commands

## Visual Indicators

### Event Log Icons
- `✓` Command executed successfully (green)
- `✗` Error occurred (red)
- `⚡` Processor event (blue)
- `→` Handler action (cyan)
- `•` Other events (white)

### Panel States
- **No identity**: Dimmed text prompts to create identity
- **Has identity**: Shows ID and network, enables key creation
- **Keys section**: Lists transit and group keys with hints

## Example Workflow

1. Start the demo in TUI mode
2. Click on Identity 1's input field
3. Type `/create` and press Enter
4. See the identity created in the panel
5. Type `/transit` to create a transit key
6. Type `/group mygroup` to create a named group key
7. Watch the State Inspector update in real-time
8. See all protocol events in the Protocol Events log
9. Press `Tab` to switch to another panel
10. Press `?` to see all keyboard shortcuts

## Debugging Features

- **State Inspector**: Shows database state summary with counts
- **Protocol Events**: Shows all handler actions and state changes
- **Refresh Button**: Force refresh all views
- **Clear Log**: Clear the event log to reduce clutter

The demo provides a developer-friendly interface for understanding the Quiet protocol's event processing pipeline while maintaining a clean, modern user experience.