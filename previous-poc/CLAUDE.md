# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common Development Commands

### Testing
```bash
# Run all tests in the protocol
cd protocols/quiet
python -m pytest

# Run specific test file
python -m pytest tests/handlers/test_validate.py

# Run with verbose output
python -m pytest -v

# Run tests with specific marker
python -m pytest -m unit
python -m pytest -m integration
```

### Type Checking
```bash
# Run mypy type checker from project root
mypy

# Check specific files
mypy core/ protocols/
```

### Running the Demo
```bash
# Run TUI demo (interactive UI)
python protocols/quiet/demo/demo.py

# Run in CLI mode
python protocols/quiet/demo/demo.py --cli

# Run with specific commands (useful for self-QA)
python protocols/quiet/demo/demo_v2.py --cli --commands "/create Alice" "/network test-network" # (or see: /help)

# Reset terminal if mouse controls get stuck (after TUI mode)
./protocols/quiet/demo/reset_terminal.sh
# Or manually: printf '\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?25h'
```

## High-Level Architecture

### Envelope-Centric Design
The system uses an **envelope-centric architecture** where all data flows through envelopes that are processed by handlers in a pipeline. Key concepts:

1. **Envelopes**: Containers for event data with metadata fields like `event_plaintext`, `event_blob`, `event_type`, `deps` (dependencies), and pipeline state flags.

2. **Handlers**: Pure functions that subscribe to envelopes based on filters. Located in `protocols/quiet/handlers/`:
   - `resolve_deps.py`: Resolves envelope dependencies from the database
   - `event_crypto.py`: Handles event-layer encryption/decryption
   - `transit_crypto.py`: Handles transit-layer encryption
   - `signature.py`: Signs/verifies events
   - `validate.py`: Validates event structure
   - `project.py`: Projects events to database state
   - `event_store.py`: Stores validated events

3. **Event Types**: Protocol-specific event definitions in `protocols/quiet/events/`:
   - Each event type (identity, message, group, channel, etc.) has flows, validators, projectors, and queries
   - Flows orchestrate emit + query using `FlowCtx.emit_event` and return `{ids, data}`
   - Projectors convert events to database deltas without direct DB access

### Database Access Rules
- **Event functions** (commands, validators, projectors): NO database access - pure functions only
- **Handlers**: Full read/write database access via `db: sqlite3.Connection` parameter
- **Queries**: Read-only access via `@query` decorator and `ReadOnlyConnection`

### Core Framework
The `/core` directory contains protocol-agnostic framework code:
- `pipeline.py`: Pipeline runner that processes envelopes through handlers
- `handlers.py`: Base handler class and registry
- `api.py`: API client that uses OpenAPI spec for operation discovery
- `db.py`: Database initialization and connection management
  (legacy command registry removed; flows register via `@flow_op`)

### Protocol Implementation
The `/protocols/quiet` directory contains the Quiet protocol implementation:
- `openapi.yaml`: OpenAPI specification defining all operations
- Event types define the protocol's data model and behavior
- Handlers implement the processing pipeline
- Each handler has an associated `.sql` file for schema initialization

## Testing Approach
Tests are organized by component type:
- `tests/handlers/`: Handler-specific tests
- `tests/integration/`: Full pipeline integration tests
- `tests/events/`: Event type command and validation tests

All tests use pytest with markers for categorization (unit, integration, handler, event_type).
