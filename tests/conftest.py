"""Pytest configuration and fixtures for all tests."""
import pytest
import sqlite3
import logging
from db import Database
import tick
import jobs

# Disable all logging during tests for performance
# Use pytest -o log_cli=true to re-enable if needed for debugging
logging.disable(logging.CRITICAL)


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset all global state before each test to ensure test isolation.

    This fixture runs automatically before every test function.
    """
    # Reset network configuration
    import network_config
    network_config.reset_network_config()

    # Reset tick job state (database-backed, needs a temp db)
    # Note: Each test creates its own DB, but we need to reset the
    # job state for tests that reuse databases across ticks
    # This is handled per-test by calling tick.reset_state(db)

    yield
