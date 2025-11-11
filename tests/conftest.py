"""Pytest configuration and fixtures for all tests."""
import pytest
import sqlite3
from db import Database
import tick
import jobs


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset all global state before each test to ensure test isolation.

    This fixture runs automatically before every test function.
    """
    # Reset network configuration
    import network_config
    network_config.reset_network_config()

    # Set job frequencies to run every tick (jobs run when elapsed >= 1ms)
    # This makes tests behave like the old tick system where everything ran every tick
    jobs.set_frequency_multiplier(0.0002)  # 5000ms * 0.0002 = 1ms

    # Reset tick job state (database-backed, needs a temp db)
    # Note: Each test creates its own DB, but we need to reset the
    # job state for tests that reuse databases across ticks
    # This is handled per-test by calling tick.reset_state(db)

    yield

    # Cleanup after test (restore production frequencies)
    jobs.set_frequency_multiplier(1.0)
