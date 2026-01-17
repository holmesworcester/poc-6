# Documentation Index

## Core Reference

- **[ideal_protocol_design.md](./ideal_protocol_design.md)** - Complete protocol specification and design (primary reference)

## Planning Documents

Active design docs and implementation plans in `planning/`:

### Identity & Linking
- **[network-root-linking-design.md](./planning/network-root-linking-design.md)** - Network creation and uniform invite process
- **[multi-device-linking-impl-plan.md](./planning/multi-device-linking-impl-plan.md)** - Multi-device support implementation
- **[isomorphic-peer-linking-plan.md](./planning/isomorphic-peer-linking-plan.md)** - Uniform peer linking design
- **[joining-linking-simplification-plan.md](./planning/joining-linking-simplification-plan.md)** - Join/link unification plan
- **[sync-connection-scoping-design.md](./planning/sync-connection-scoping-design.md)** - Per-peer sync connection handling

### Security & Permissions
- **[admin-access-design.md](./planning/admin-access-design.md)** - Admin access to private channels
- **[removal-enforcement-design.md](./planning/removal-enforcement-design.md)** - User and peer removal enforcement
- **[forward-secrecy-plan.md](./planning/forward-secrecy-plan.md)** - Forward secrecy architecture
- **[forward-secrecy-cli-plan.md](./planning/forward-secrecy-cli-plan.md)** - Forward secrecy CLI commands

### Infrastructure
- **[bootstrap-simplification-plan.md](./planning/bootstrap-simplification-plan.md)** - Bootstrap process refactoring
- **[tick-jobs-refactor-plan.md](./planning/tick-jobs-refactor-plan.md)** - Jobs system refactoring
- **[event-registry-design.md](./planning/event-registry-design.md)** - Event type registration pattern

### Features
- **[disappearing-messages-spec.md](./planning/disappearing-messages-spec.md)** - Disappearing messages feature
- **[file-features-spec.md](./planning/file-features-spec.md)** - File attachment features
- **[focused-file-sync-spec.md](./planning/focused-file-sync-spec.md)** - Focused file sync implementation

## Archive

Historical and completed docs in `archive/`:

- **cli-architecture-rules.md** - CLI architecture rules (implemented)
- **cli-prototype-design.md** - CLI design prototype (implemented)
- **cli-bugs-fixed.md** - CLI bugs (all fixed)
- **device-names-plan-superseded.md** - Device names plan (superseded)
- **test-status-snapshot.md** - Test status snapshot
- **todo-device-name-outdated.md** - Old device name TODO
- **convergence-testing-historical.md** - Convergence testing notes
- Plus other historical notes

---

## Quick Reference

### Running Tests
```bash
./run_tests.sh                           # All tests
./run_tests.sh tests/test_file.py        # Specific file
./run_tests.sh -k test_pattern           # Pattern match
```

### Jobs and Tick System

The `tick()` function in `tick.py` runs all periodic jobs:

```python
from tick import tick
tick(t_ms=1000, db=db)  # Run one cycle at time t_ms
```

Jobs are defined in `jobs.py`:
- `sync_send` - Send sync requests to all peers
- `sync_receive` - Process incoming sync responses

### Structured Logging

Format: `[TAG] key1=value1 key2=value2 ...`

Key tags: `[BOOTSTRAP_SEND]`, `[SYNC_RECEIVE]`, `[UNWRAP_START]`, `[UNWRAP_TRANSIT_KEY]`

Filter logs: `grep '\[UNWRAP'`
