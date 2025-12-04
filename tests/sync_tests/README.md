# Stubbed Connection Test Suite (WIP)

This directory contains a work-in-progress test suite for isolated sync protocol testing.

## Status: Archived/WIP

These tests were developed alongside the negentropy sync implementation but are not yet
fully integrated. They are preserved here for future use if we run into problems testing
sync and need a more isolated testing approach.

## What's Here

- `stub_connection.py` - Stub connection bridge that bypasses encryption/handshakes
- `sync_harness.py` - Test harness for running sync rounds with rich visibility
- `event_factory.py` - Factory for generating test events with various patterns
- `test_negentropy_isolated.py` - Tests for negentropy sync protocol in isolation

## Known Issues

1. Edge case tests fail because convergence detection doesn't handle empty peers
2. The stub bridge needs to be wired more tightly to the negentropy sync_connection() API
3. Tests assume blob transfer happens in isolation, but negentropy only exchanges IDs

## To Resurrect

1. Update `sync_harness.py` to handle empty peer convergence
2. Wire stub bridge to properly route negentropy protocol messages
3. Add actual blob delivery simulation (or accept that isolated tests only verify protocol exchange)

## Main Negentropy Tests

The primary negentropy tests are in the parent tests directory:
- `tests/test_negentropy.py` - Unit tests for negentropy module
- `tests/test_negentropy_algo.py` - Algorithm tests for RBSR protocol
