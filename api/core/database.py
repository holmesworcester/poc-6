"""Database management for the API.

Provides database connection and peer context management.
The API runs for a single peer, so we store the peer_id at startup.

In test mode, peer_id can be overridden per-request via X-Test-Peer-Id header.
"""

import os
import sqlite3
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from core.db import Database, create_safe_db, create_unsafe_db, SafeDB, UnsafeDB
from core import schema

# Global database instance
_db: Database | None = None
_peer_id: str | None = None
_test_mode: bool = False

# Context variable for per-request peer_id override (used in test mode)
_request_peer_id: ContextVar[str | None] = ContextVar("request_peer_id", default=None)
_request_t_ms: ContextVar[int | None] = ContextVar("request_t_ms", default=None)

# Default database path
DEFAULT_DB_PATH = os.environ.get("QUIET_DB_PATH", ":memory:")


def init_db(db_path: str = DEFAULT_DB_PATH, peer_id: str | None = None) -> Database:
    """Initialize the database connection.

    Args:
        db_path: Path to SQLite database file, or ":memory:" for in-memory.
        peer_id: The peer_id this API instance is running for.
                 If None, must be set later via set_peer_id().

    Returns:
        The Database instance.
    """
    global _db, _peer_id

    # Skip if already initialized (for tests that inject their own db)
    if _db is not None:
        if peer_id:
            _peer_id = peer_id
        return _db

    conn = sqlite3.connect(db_path, check_same_thread=False)
    # Performance optimizations
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
    conn.execute("PRAGMA temp_store = MEMORY")

    _db = Database(conn)
    schema.create_all(_db)

    if peer_id:
        _peer_id = peer_id

    return _db


def inject_db(db: Database) -> None:
    """Inject an existing database instance (for testing).

    This allows tests to share a database with the API.
    """
    global _db
    _db = db


def reset_db() -> None:
    """Reset database state (for testing between tests)."""
    global _db, _peer_id, _test_mode
    _db = None
    _peer_id = None
    _test_mode = False


def get_db() -> Database:
    """Get the database instance."""
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _db


def set_peer_id(peer_id: str) -> None:
    """Set the peer_id for this API instance."""
    global _peer_id
    _peer_id = peer_id


def get_peer_id() -> str:
    """Get the peer_id for this API instance.

    In test mode, checks for per-request override first.
    """
    # In test mode, check for per-request override
    if _test_mode:
        override = _request_peer_id.get()
        if override:
            return override

    if _peer_id is None:
        raise RuntimeError("Peer ID not set. Call set_peer_id() first.")
    return _peer_id


def enable_test_mode() -> None:
    """Enable test mode, allowing per-request peer_id override."""
    global _test_mode
    _test_mode = True


def set_request_peer_id(peer_id: str | None) -> None:
    """Set peer_id for the current request context (test mode only)."""
    _request_peer_id.set(peer_id)


def set_request_t_ms(t_ms: int | None) -> None:
    """Set timestamp for the current request context (test mode only)."""
    _request_t_ms.set(t_ms)


def get_safe_db() -> SafeDB:
    """Get a SafeDB scoped to the current peer."""
    return create_safe_db(get_db(), get_peer_id())


def get_unsafe_db() -> UnsafeDB:
    """Get an UnsafeDB for device-wide operations."""
    return create_unsafe_db(get_db())


def get_t_ms() -> int:
    """Get current timestamp in milliseconds.

    In test mode, checks for per-request override first.
    For now, just returns system time. In production, this might need
    to be coordinated with the sync system.
    """
    # In test mode, check for per-request override
    if _test_mode:
        override = _request_t_ms.get()
        if override is not None:
            return override

    import time
    return int(time.time() * 1000)


def from_urlsafe_b64(s: str) -> str:
    """Convert URL-safe base64 to standard base64.

    URL-safe base64 uses - instead of + and _ instead of /.
    This function converts them back to standard base64.
    """
    return s.replace('-', '+').replace('_', '/')


def verify_network_access(network_id: str) -> str:
    """Verify the current peer has access to the given network.

    Returns the peer_id if access is granted.
    Raises HTTPException(404) if network not found or no access.

    Note: network_id may be in URL-safe base64 format (using - and _ instead of + and /).
    """
    from fastapi import HTTPException
    from events.identity import network

    # Convert from URL-safe base64 if needed
    network_id = from_urlsafe_b64(network_id)

    peer_id = get_peer_id()
    db = get_db()

    peer_network_id = network.get_network_id_for_peer(peer_id, db)

    if not peer_network_id or peer_network_id != network_id:
        raise HTTPException(status_code=404, detail="Network not found")

    return peer_id
