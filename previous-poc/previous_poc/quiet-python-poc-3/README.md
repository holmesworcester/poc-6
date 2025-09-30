# quiet-python-poc-3

Event-driven P2P messaging framework simulating Tor-like secure communication.

## Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Quick Start

```bash
# Run tests
python core/test_runner.py protocols/message_via_tor

# Run API server
python core/api.py protocols/message_via_tor

# Run demo UI
python protocols/message_via_tor/demo/demo.py

# Run signed groups demo
python protocols/signed_groups/demo/demo.py
```

## Structure

```
├── core/                      # Framework core
│   ├── test_runner.py        # JSON test executor
│   ├── api.py                # REST API server
│   ├── tick.py               # Event processor
│   └── docs/                 # Core documentation
│       ├── framework.md      # Framework design & architecture
│       └── type_safety.md    # Type safety implementation plan
├── protocols/                 # Protocol implementations
│   └── message_via_tor/      # P2P messaging protocol
│       ├── handlers/         # Event handlers
│       ├── demo/             # Terminal UI demo
│       ├── docs.md           # Protocol documentation
│       └── schema.sql        # Database schema
```

## Key Concepts

- **Events**: All actions are events (identity creation, messages, etc.)
- **Handlers**: Process events and update state
- **Tick**: Background job processor for message delivery
- **Demo**: Interactive terminal UI showing multiple identities

## Scenario Tests

API-level scenario tests validate end-to-end workflows. Place them in `protocols/<name>/scenarios/*.json`:

```json
{
  "scenarios": {
    "scenario_name": {
      "description": "What this tests",
      "steps": [
        {
          "name": "step_name",
          "request": {
            "method": "POST",
            "path": "/endpoint",
            "body": { "field": "value" }
          },
          "assertions": {
            "status": 200,
            "body.field": "expected",
            "body.items": { "$length": 2 },
            "body.items[0].name": "first",
            "body.list[*].id": { "$contains": "123" },
            "capture": {
              "var_name": "$.field.id"
            }
          }
        },
        {
          "name": "use_captured_var",
          "request": {
            "method": "GET", 
            "path": "/endpoint/${var_name}?param=value"
          },
          "assertions": {
            "status": 200
          }
        }
      ]
    }
  }
}
```

Features:
- **Variable capture**: Store values with `capture` for use in later steps via `${var}`
- **Path assertions**: Use dot notation (`body.field`) or brackets (`body[0]`)
- **Array assertions**: `{"$length": N}` for exact length, `{"$contains": value}` for inclusion
- **Query params**: Append `?key=value` to paths or use separate `params` field

## Using a virtual environment (venv)

A helper script is included to create and populate a virtual environment with
runtime dependencies. Run:

```bash
./scripts/setup_venv.sh
source venv/bin/activate
```

If this environment already contains a venv directory you can activate it
directly with `source venv/bin/activate`.