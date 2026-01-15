"""Pytest configuration and fixtures for all tests."""
import pytest
import sqlite3
import logging
from core.db import Database
from core import schema
from core import tick
from core import jobs

# Disable all logging during tests for performance
# Use pytest -o log_cli=true to re-enable if needed for debugging
logging.disable(logging.CRITICAL)


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset all global state before each test to ensure test isolation.

    This fixture runs automatically before every test function.
    """
    # Reset network configuration
    from core import network_config
    network_config.reset_network_config()

    # Reset tick job state (database-backed, needs a temp db)
    # Note: Each test creates its own DB, but we need to reset the
    # job state for tests that reuse databases across ticks
    # This is handled per-test by calling tick.reset_state(db)

    yield


@pytest.fixture
def fresh_db():
    """Create a fresh in-memory database with all tables initialized.

    Eliminates boilerplate setup:
        conn = sqlite3.Connection(":memory:")
        db = Database(conn)
        schema.create_all(db)
    """
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)
    return db


@pytest.fixture
def fresh_db_with_alice(fresh_db):
    """Create a fresh database with Alice's network already set up.

    Eliminates repeated 5-line setup pattern from test_forward_secrecy.py,
    test_user_removal.py, and many others.

    Usage:
        def test_something(fresh_db_with_alice):
            db, alice = fresh_db_with_alice
            # Use alice['peer_id'], alice['channel_id'], etc.
    """
    from events.identity import user
    db = fresh_db
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()
    return db, alice


@pytest.fixture
def fresh_db_with_alice_and_bob(fresh_db_with_alice):
    """Create a fresh database with Alice's network and Bob joined.

    Further eliminates test setup for multi-peer scenarios.

    Usage:
        def test_something(fresh_db_with_alice_and_bob):
            db, alice, bob = fresh_db_with_alice_and_bob
    """
    from events.identity import user as user_module
    from events.identity import invite as invite_module
    from events.identity import peer as peer_module
    from tests.utils import tick_helper

    db, alice = fresh_db_with_alice

    invite_id, invite_link, invite_data = invite_module.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    bob_peer_id = peer_module.create(t_ms=2000, db=db)
    bob = user_module.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Sync to converge
    tick_helper.sync_until_converged(db=db, start_t_ms=3000, max_rounds=200, check_interval=1)

    return db, alice, bob


@pytest.fixture
def perf_db(tmp_path):
    """Create a file-based database for performance testing.

    Unlike fresh_db which uses :memory:, this writes to a real file to test
    actual disk I/O performance. Uses WAL mode and performance pragmas.

    Usage:
        def test_perf(perf_db):
            db = perf_db
            # Operations hit real disk
    """
    db_path = tmp_path / "perf_test.db"
    conn = sqlite3.connect(str(db_path))
    db = Database(conn)

    # Performance optimizations (WAL already enabled in Database.__init__)
    conn.execute("PRAGMA synchronous = NORMAL")  # Faster than FULL, still safe
    conn.execute("PRAGMA cache_size = -64000")   # 64MB cache
    conn.execute("PRAGMA temp_store = MEMORY")

    schema.create_all(db)
    yield db
    conn.close()
