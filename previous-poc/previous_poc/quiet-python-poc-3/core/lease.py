"""
Simple SQLite-based lease helper for singleton/background jobs.

Tables are created in the connected database and are safe to use across
multiple connections/processes within the same SQLite file.
"""
import sqlite3


def init_leases(conn: sqlite3.Connection):
    """Create the _leases table if it doesn't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _leases (
            name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            expires_at_ms INTEGER NOT NULL
        )
        """
    )
    conn.commit()


def acquire_lease(conn: sqlite3.Connection, name: str, owner: str, now_ms: int, ttl_ms: int) -> bool:
    """
    Try to acquire a lease. Returns True on success, False otherwise.
    - Succeeds if no row exists or if existing row is expired.
    - Sets expires_at_ms = now_ms + ttl_ms.
    """
    init_leases(conn)
    cur = conn.cursor()
    # Use a single transaction to avoid races
    cur.execute("BEGIN IMMEDIATE")
    try:
        row = cur.execute("SELECT owner, expires_at_ms FROM _leases WHERE name = ?", (name,)).fetchone()
        if row is None or row[1] <= now_ms:
            cur.execute(
                "REPLACE INTO _leases(name, owner, expires_at_ms) VALUES(?, ?, ?)",
                (name, owner, now_ms + ttl_ms)
            )
            conn.commit()
            return True
        # Already leased and not expired
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise


def renew_lease(conn: sqlite3.Connection, name: str, owner: str, now_ms: int, ttl_ms: int) -> bool:
    """Renew a held lease by the same owner. Returns True on success."""
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        updated = cur.execute(
            "UPDATE _leases SET expires_at_ms = ? WHERE name = ? AND owner = ?",
            (now_ms + ttl_ms, name, owner)
        ).rowcount
        if updated:
            conn.commit()
            return True
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise


def release_lease(conn: sqlite3.Connection, name: str, owner: str) -> bool:
    """Release a lease if held by owner. Returns True if released."""
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        deleted = cur.execute(
            "DELETE FROM _leases WHERE name = ? AND owner = ?",
            (name, owner)
        ).rowcount
        if deleted:
            conn.commit()
            return True
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise

