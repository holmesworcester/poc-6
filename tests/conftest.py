"""Pytest configuration and fixtures for all tests."""
import pytest
import sqlite3
import logging
import uuid
import os
import shutil
from pathlib import Path
from core.db import Database
from core import schema
from core import tick
from core import jobs

# Disable all logging during tests for performance
# Use pytest -o log_cli=true to re-enable if needed for debugging
logging.disable(logging.CRITICAL)

# Real disk path for disk-based tests (NOT tmpfs)
# Uses project directory which is on real disk
_DISK_TEST_DIR = Path(__file__).parent.parent / ".test_dbs"


def _cleanup_test_dbs():
    """Clean up test database directory."""
    if _DISK_TEST_DIR.exists():
        shutil.rmtree(_DISK_TEST_DIR)


# Clean up at import time (before any tests run)
_cleanup_test_dbs()


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset all global state before each test to ensure test isolation.

    This fixture runs automatically before every test function.
    """
    # Reset network configuration
    from core import network_config
    network_config.reset_network_config()

    # Reset job frequency multiplier
    jobs.reset_frequency_multiplier()

    # Reset tick job state (database-backed, needs a temp db)
    # Note: Each test creates its own DB, but we need to reset the
    # job state for tests that reuse databases across ticks
    # This is handled per-test by calling tick.reset_state(db)

    yield

    # Also reset after test to catch tests that modify global state
    jobs.reset_frequency_multiplier()
    network_config.reset_network_config()


@pytest.fixture
def fresh_db():
    """Create a file-based database on REAL DISK with WAL mode.

    Default for all tests - uses real disk I/O for realistic performance.
    WAL mode makes it nearly as fast as in-memory while testing real I/O paths.

    For pure in-memory testing, use mem_db instead.
    """
    _DISK_TEST_DIR.mkdir(exist_ok=True)
    db_path = _DISK_TEST_DIR / f"test_{uuid.uuid4().hex}.db"
    conn = sqlite3.connect(str(db_path))
    db = Database(conn)

    # Performance optimizations (WAL already enabled in Database.__init__)
    conn.execute("PRAGMA synchronous = NORMAL")  # Faster than FULL, still safe
    conn.execute("PRAGMA cache_size = -64000")   # 64MB cache
    conn.execute("PRAGMA temp_store = MEMORY")

    schema.create_all(db)
    yield db
    conn.close()

    # Cleanup DB files
    for suffix in ["", "-wal", "-shm"]:
        try:
            os.unlink(str(db_path) + suffix)
        except FileNotFoundError:
            pass


@pytest.fixture
def mem_db():
    """Create a fresh in-memory database.

    Use this when you explicitly need in-memory behavior (faster but less realistic).
    Most tests should use fresh_db (disk-based) for realistic I/O testing.
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
    from tests.utils.tick_helper import run_ticks

    db, alice = fresh_db_with_alice

    invite_id, invite_link, invite_data = invite_module.create(
        peer_id=alice['peer_id'],
        t_ms=1500,
        db=db
    )

    bob_peer_id = peer_module.create(t_ms=2000, db=db)
    bob = user_module.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Sync to converge - 15 rounds is enough for 2-peer connection
    run_ticks(db=db, start_t_ms=3000, num_rounds=15)

    return db, alice, bob


# Aliases for backwards compatibility
disk_db = fresh_db
perf_db = fresh_db
wal_db = fresh_db
