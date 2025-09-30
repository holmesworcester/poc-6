# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an event-driven P2P messaging framework with a simulated Tor-like network. The project implements secure messaging protocols using an event-sourcing architecture with handler-based event processing.

## Key Commands

use venv!!!

```bash
# Setup virtual environment and install dependencies
python -m venv venv
source venv/bin/activate
pip install pyyaml textual rich pynacl

# Run tests for a specific protocol
python core/test_runner.py protocols/message_via_tor
python core/test_runner.py protocols/framework_tests

# Prefer snapshot assertions (for SQL-native protocols)
# When set, command tests require `then.snapshot` blocks instead of `then.db`.
SNAPSHOT_ONLY=1 python core/test_runner.py protocols/message_via_tor

# Run API server
python core/api.py protocols/message_via_tor

# Run terminal UI demo
python protocols/message_via_tor/demo/demo.py

# Run demo CLI (new)
python demo_cli.py

# Generate test blobs
python generate_test_blobs.py
```

## Architecture Overview

### Core Framework (`core/`)
- **Event-Driven Architecture**: All state changes happen through events stored in an event store
- **Handler System**: Each handler processes specific event types and updates state via projectors
- **Test Runner**: Language-neutral JSON/YAML-based test executor
- **API Server**: Maps OpenAPI operations to handler commands
- **Database**: SQLite-based persistent storage (new db.py module)
- **Crypto Module**: Thin wrapper around PyNaCl for cryptographic operations

### Protocol Structure
Each protocol in `protocols/` contains:
- `handlers/`: Event handlers with YAML/JSON configs (Note: YAML experimental and unused), projectors, and command implementations
- `openapi.yaml`: API specification
- `docs.md`: Protocol documentation
- `demo/`: Optional demo implementations

### Handler Pattern
Each handler directory contains:
- `*_handler.yaml/json`: Configuration with test scenarios
- `projector.py`: Projects events to derive state
- Command files (e.g., `create.py`, `list.py`): Execute operations
- Jobs run by tick processor for background tasks

### Testing Approach
- JSON-based test specifications in handler configs
- Tests use given/then scenarios with event sequences
- Permutation testing ensures order independence
- Run specific tests with: `python core/test_runner.py <protocol_path> --test <test_name>`
- All protocol-specific tests should be as JSON
- Real crypto tests should generate actual encrypted values and put these in tests (dummy crypto is an option too when not testing crypto)
- For SQL-native protocols, prefer asserting `then.snapshot` (not `then.db`) to decouple from dict-state and allow pure-SQL projectors/reads.

## Development Guidelines

### Event Envelope Format
```python
{
    "payload": {...},  # The actual event data
    "metadata": {
        "selfGenerated": bool,
        "received_by": str,
        "eventId": str,
        "timestamp": str
    }
}
```

### Adding New Handlers
1. Create handler directory in protocol's `handlers/` folder
2. Add `*_handler.yaml` with configuration and tests
3. Implement `projector.py` for state derivation
4. Add command files for operations
5. Update protocol's `openapi.yaml` if adding API endpoints

### Database Integration
The project now uses SQLite for persistent storage. The `core/db.py` module provides database functionality. Handler commands receive a `db` parameter for database operations.

### Important Design Principles
- **Local-First**: Each identity maintains its own state view
- **Eventually Consistent**: Events can be processed in any order
- **Protocol Agnostic**: Core framework separate from protocol implementations
- **Test-First**: Write tests in handler YAML before implementation
- **Real Crypto**: Uses actual cryptographic operations, not simulated
- **Core/Protocol Separation**: No protocol-specific logic should be in "core", ever.
- **LLM-testable**: all UI work (e.g. demo.py) should be LLM-testable via CLI or scripts.

## Current Focus Areas
Based on recent commits, the project is transitioning from in-memory to persistent storage using SQLite. Many protocol-specific elements are being refactored out of the core framework to maintain clean separation of concerns.

## Upcoming Improvements
- Add a way for projector tests to opt out of idempotency checks

## Self QA
- 