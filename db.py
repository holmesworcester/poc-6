"""Database wrapper and utilities.

Convention for db queries:
- db.query() or db.query_all() returns list[dict[str, Any]] for multiple rows
- db.query_one() returns dict[str, Any] | None for single row (None if not found)
- db.execute() for INSERT/UPDATE/DELETE, returns None
- All rows returned as dicts with column names as keys
"""
import sqlite3
from typing import Any


class Database:
    """Wrapper around sqlite3 connection for convenient query methods."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        # Enable WAL mode for better concurrency and performance
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute query and return all rows as list of dicts."""
        cursor = self._conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Alias for query() - execute query and return all rows as list of dicts."""
        return self.query(sql, params)

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """Execute query and return single row as dict, or None if not found."""
        cursor = self._conn.execute(sql, params)
        row = cursor.fetchone()
        return dict(row) if row else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute INSERT/UPDATE/DELETE statement."""
        self._conn.execute(sql, params)

    def execute_returning(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute UPDATE/INSERT/DELETE with RETURNING clause and return results."""
        cursor = self._conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()

    def changes(self) -> int:
        """Return number of rows affected by last execute()."""
        return self._conn.total_changes
