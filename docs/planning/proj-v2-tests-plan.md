# Projection v2 Tests Plan

Branch: proj-v2-tests
Worktree: /home/holmes/poc-6-proj-v2-tests

## Goal
Create a minimal test harness and helpers for projector-level TDD, while relying
on existing scenario tests for broader coverage.

## Scope
- tests/projection/
- shared helper utilities for v2 vs legacy comparison

Out of scope:
- core/projection implementation
- recorded dispatch changes

## Plan
1. Create tests/projection/
   - Add a small fixture that builds a minimal DB state
   - Provide helpers to run legacy project() and v2 project_pure() for the same event

2. Provide example parity and harness tests
   - One or two minimal comparisons to show the helper pattern
   - Keep event-specific parity tests in the pilot conversion worktrees

3. Block and reject tests
   - Missing deps -> resolver returns block
   - Invalid signature -> resolver returns reject
   - Missing signer_type -> resolver returns reject

4. Keep tests deterministic and minimal
   - Avoid heavy integration; focus on projection tables

## Deliverables
- Test harness + example parity tests
- Block/reject coverage for resolver behavior

## Verification
- PYTHONPATH=. pytest tests/projection -v
- Prefer running scenario tests when possible (tests/scenario_tests)
