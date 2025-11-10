"""Pytest configuration and fixtures for all tests."""
import pytest


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset all global state before each test to ensure test isolation.

    This fixture runs automatically before every test function.
    """
    # Reset network configuration
    import network_config
    network_config.reset_network_config()

    # Reset sync receive call counter
    from events.transit import sync
    sync._receive_call_count = 0

    yield

    # Cleanup after test (if needed)
    pass
