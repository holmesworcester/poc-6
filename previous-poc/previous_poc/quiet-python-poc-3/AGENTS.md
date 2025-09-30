# Repository Guidelines

## Project Structure & Module Organization
- `core/`: Protocol-agnostic framework (`api.py`, `command.py`, `db.py`, `tick.py`, `test_runner.py`). Keep business logic for specific protocols out of here.
- `protocols/`: Protocol implementations. Example `protocols/message_via_tor/` contains:
  - `handlers/` (request handlers), `schema.sql`, `api.yaml`, `demo/` (TUI + tests).
- `scripts/`: Developer utilities (e.g., `setup_venv.sh`).
- Top-level `test_*.py`: Targeted checks; protocol demo tests live under `protocols/*/demo/`.

## Build, Test, and Development Commands
- Create/refresh venv: `./scripts/setup_venv.sh && source venv/bin/activate`.
- Run framework test runner (all tests for a protocol):
  - `python core/test_runner.py protocols/message_via_tor`
  - Specific test: `python core/test_runner.py protocols/message_via_tor --test <name>`
- Run pytest suite:
  - All: `pytest -q`
  - Demo-only: `pytest protocols/message_via_tor/demo/test_demo.py -q`
- Demo TUI: `python protocols/message_via_tor/demo/demo.py`
- Direct API call (example):
  - `python core/api.py message_via_tor POST /identities --data '{"name":"Alice"}'`

## Coding Style & Naming Conventions
- Python 3; follow PEP 8. Use 4-space indents and a soft 88–120 column limit.
- Prefer small, pure functions; keep `core/` protocol-agnostic.
- Naming: modules/files `snake_case.py`; classes `PascalCase`; functions/vars `snake_case`.
- Add docstrings for public functions; include type hints where practical.
- Ignore YAML handlers. 

## Testing Guidelines
- Framework: YAML/JSON-driven tests via `core/test_runner.py` using `protocols/*/api.yaml` and `handlers/`.
- Ignore YAML handlers. 
- Focus: for every unit of work on handlers, create/update `core/test_runner.py` tests and run them before/after changes. Treat failing runner tests as blockers. Tests are JSON in handlers. 
- UI/flow: `pytest` tests under `protocols/*/demo/` and top-level `test_*.py`.
- Deterministic tests: set `CRYPTO_MODE=dummy`; use per-test DBs via `TEST_DB_PATH`.
- Naming: files start with `test_`; keep tests close to the code they verify.

## Commit & Pull Request Guidelines
- Commits: imperative, concise subjects (e.g., "add tick job isolation"); include scope when useful (core, protocol, demo).
- PRs include: summary, rationale, affected modules/paths, test coverage notes, reproduction steps; link issues. Add screenshots/GIFs for demo UI changes.

## Security & Configuration Tips
- Use `CRYPTO_MODE=dummy` if it makes tests clearer, but also test with real crypto when the functionality depends on encryption.
- Create real crypto tests by using outputs from previous tests. 
- Local SQLite DBs are disposable; prefer per-test DBs via `TEST_DB_PATH`. Useful env vars: `CRYPTO_MODE`, `API_DB_PATH`, `HANDLER_PATH`, `TEST_DB_PATH`.
