# Quiet Protocol - POC #6

A proof-of-concept implementation of the [Quiet Protocol](./docs/quiet-protocol-specification.md): an end-to-end encrypted, peer-to-peer protocol for team chat (like Slack), designed to be simple enough to implement as a "weekend project."

## Overview

This codebase implements an event-sourced, eventually-consistent messaging system with:

- **End-to-end encryption** using libsodium primitives
- **Multi-device support** with linked peers per user
- **Forward secrecy** via key rotation and purging
- **Decentralized sync** using bloom-filtered set reconciliation
- **Admin controls** for user invites and permissions

## Quick Start

### Prerequisites

- Python 3.11+
- SQLite 3

### Running the CLI

```bash
python cli.py
```

### Running Tests

```bash
./run_tests.sh                           # All tests
./run_tests.sh tests/scenario_tests/     # Scenario tests only
./run_tests.sh -k test_pattern           # Match pattern
```

## Architecture

```
events/                 # Event types and projectors
  identity/             # Network, users, peers, invites, admins
  group/                # Group membership and keys
  content/              # Messages, channels, files, reactions
  network/              # Sync protocol, transit keys, addressing

simulator/              # Network simulation for testing

tests/
  scenario_tests/       # End-to-end API tests
  cli/                  # CLI command tests

docs/
  quiet-protocol-specification.md  # Protocol specification
  planning/                        # Design docs and plans
  archive/                         # Historical documents
```

### Key Concepts

**Events**: All state changes are expressed as immutable events stored in a single SQLite database. Events are projected into queryable tables.

**Peers**: Each device has a unique peer identity. Multiple peers can be linked to the same user across devices.

**Recording**: Events are "recorded" per-peer, allowing multiple local peers to have independent views of network state.

**Sync**: Peers exchange events via bloom-filtered sync requests/responses. Events propagate through the mesh until all peers converge.

## Documentation

- **[Protocol Specification](./docs/quiet-protocol-specification.md)** - Complete protocol design
- **[Documentation Index](./docs/README.md)** - All docs organized by topic

## Design Principles

1. **Event-sourced**: All non-ephemeral state comes from events
2. **API-only access**: CLI uses only event module functions, never raw SQL
3. **Deterministic testing**: All timestamps are explicit parameters
4. **Single source of truth**: Events are canonical; projections are derived
5. **Signed events**: All shared events should be signed, except for file_slice

## Status

This is proof-of-concept #6, focused on validating the core protocol design. See `docs/planning/` for active development plans.
