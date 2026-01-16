"""Projection v2 test harness and helpers.

This package provides:
- Fixtures for building minimal DB state for v2 projection tests
- Helpers for comparing legacy project() vs v2 project_pure() outcomes
- Example tests showing block/reject/parity patterns

Usage:
    from tests.projection_v2.helpers import compare_projection, build_minimal_state
    from tests.projection_v2.conftest import fresh_v2_db  # pytest fixture
"""
