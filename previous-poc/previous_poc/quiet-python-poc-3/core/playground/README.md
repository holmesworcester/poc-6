# Playground

A general-purpose TUI/CLI framework for interacting with protocols through a configurable multi-window interface.

## Usage

### TUI Mode (Interactive)
```bash
python -m core.playground.playground message_via_tor --config core/playground/playground_demo.yaml
```

### CLI Mode (LLM-testable)
```bash
# Single command
python -m core.playground.playground message_via_tor --cli "/api GET /identities"

# Batch commands from file
python -m core.playground.playground message_via_tor --cli-file core/playground/test_demo_cli.txt

# Interactive CLI
python -m core.playground.playground message_via_tor --cli-interactive
```

## Features

- **Multi-window TUI** with configurable grid layouts
- **Command aliases** for common operations
- **Variable substitution** with `/define` command
- **API integration** using the core api.py tool
- **YAML configuration** for saving layouts and settings
- **LLM-testable** CLI mode for automation

## Configuration Files

- `playground_demo.yaml` - Full demo configuration with 4 windows
- `playground_simple.yaml` - Simple 2-window chat example
- `playground_flow.yaml` - Streamlined config for the complete demo flow
- `playground_planning.md` - Original planning document

## Test Files

- `test_cli_commands.txt` - Basic CLI command examples
- `test_demo_cli.txt` - Demo workflow commands
- `demo_aliases.txt` - Shows alias functionality