# Message via Tor Demo

This demo showcases the message_via_tor protocol with both TUI (Terminal User Interface) and CLI (Command Line Interface) modes.

## Running the Demo

### TUI Mode (default)
```bash
python demo.py
```

### CLI Mode
```bash
# Run individual commands
python demo.py --run "1:/create alice" "2:/create bob" "1:/invite -> link" "2:/join charlie \$link"

# Run from script file
python demo.py --script-file scenarios/basic_messaging.script

# With options
python demo.py --run "1:/create alice" --verbose --format json
```

## CLI Commands

- `1:/create [name]` - Create identity in panel 1
- `1:/invite` - Generate invite link
- `2:/join <name> <invite>` - Join network in panel 2
- `1:message text` - Send message from panel 1
- `tick [count]` - Run tick cycles
- `refresh` - Refresh all panels
- `/help` - Show available commands

## Variable Capture

Use `->` to capture command output:
```bash
"1:/invite -> myLink"
"2:/join bob \$myLink"
```

## Scenario Scripts

Pre-built scenarios in `scenarios/`:
- `basic_messaging.script` - Simple message exchange
- `multi_network.script` - Multiple independent networks  
- `variable_capture.script` - Variable capture and substitution
- `error_handling.script` - Error cases and recovery
- `full_demo.script` - Complete feature demonstration

## Options

- `--no-reset` - Keep existing database
- `--db-path <path>` - Custom database location
- `--format json` - JSON output (CLI mode)
- `--verbose` - Show execution details
- `--stop-on-error` - Stop on first error