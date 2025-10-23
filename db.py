"""Database wrapper and utilities.

Convention for db queries:
- db.query() or db.query_all() returns list[dict[str, Any]] for multiple rows
- db.query_one() returns dict[str, Any] | None for single row (None if not found)
- db.execute() for INSERT/UPDATE/DELETE, returns None
- All rows returned as dicts with column names as keys

IMPORTANT: Do not use Database methods directly. Use create_safe_db() or create_unsafe_db()
to enforce proper scoping and prevent data leakage between peers.
"""
import re
import sqlite3
from typing import Any


# Tables that require recorded_by scoping (peer-subjective views)
SUBJECTIVE_TABLES = {
    'messages',
    'message_deletions',           # Message deletions (subjective)
    'deleted_events',              # Deleted events (prevents projection of deleted messages)
    'peers_shared',
    'peer_self',                   # Mapping from peer_id to peer_shared_id (subjective)
    'groups',
    'channels',
    'addresses',
    'group_keys',                  # NEW: subjective group encryption keys
    'group_keys_shared',           # Group keys shared (subjective)
    'group_prekeys',               # NEW: subjective group prekeys
    'group_prekeys_shared',        # Group prekeys shared (subjective)
    'transit_prekeys_shared',      # Transit prekeys shared (subjective)
    'users',
    'group_members',
    'group_members',
    'valid_events',
    'blocked_events_ephemeral',
    'blocked_event_deps_ephemeral',
    'shareable_events',
    'invites',
    'networks',                    # Networks are subjective (peer-scoped)
    'bootstrap_status',            # Bootstrap status (network creator/joiner status)
    'files',                       # File metadata (peer-scoped)
    'file_slices',                 # File slice storage (peer-scoped)
    'message_attachments',         # Message-file links (peer-scoped)
    'keys_to_purge',               # Forward secrecy: keys marked for purging (peer-scoped)
    'message_rekeys',              # Forward secrecy: rekeyed messages (peer-scoped)
}

# Tables that are device-wide (not scoped by recorded_by)
DEVICE_TABLES = {
    'local_peers',
    'transit_keys',                # NEW: device-wide transit keys
    'transit_prekeys',             # RENAMED from prekeys
    'store',
    'incoming_blobs',
    'sync_state_ephemeral',
}


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


class ScopingViolation(Exception):
    """Raised when a scoping rule is violated."""
    pass


class SafeDB:
    """Database wrapper that enforces recorded_by scoping for peer-subjective tables.

    Usage:
        safedb = create_safe_db(db, recorded_by='alice_peer_id')
        messages = safedb.query("SELECT * FROM messages WHERE channel_id = ?", ('chan1',))
        safedb.execute("INSERT INTO messages VALUES (...)", (...))

    All operations on subjective tables are validated to ensure:
    1. The table is in SUBJECTIVE_TABLES
    2. Queries include recorded_by filter
    3. All returned rows match the expected recorded_by value
    """

    def __init__(self, db: Database, recorded_by: str):
        self._db = db
        self.recorded_by = recorded_by

    def _extract_table(self, sql: str) -> str:
        """Extract table name from SQL statement."""
        sql_upper = sql.upper().strip()

        # Handle SELECT
        if 'FROM' in sql_upper:
            match = re.search(r'\bFROM\s+(\w+)', sql_upper)
            if match:
                return match.group(1).lower()

        # Handle INSERT (including INSERT OR IGNORE, INSERT OR REPLACE)
        if 'INSERT' in sql_upper:
            match = re.search(r'\bINSERT\s+(?:OR\s+(?:IGNORE|REPLACE)\s+)?INTO\s+(\w+)', sql_upper)
            if match:
                return match.group(1).lower()

        # Handle UPDATE
        if 'UPDATE' in sql_upper:
            match = re.search(r'\bUPDATE\s+(\w+)', sql_upper)
            if match:
                return match.group(1).lower()

        # Handle DELETE
        if 'DELETE FROM' in sql_upper:
            match = re.search(r'\bDELETE\s+FROM\s+(\w+)', sql_upper)
            if match:
                return match.group(1).lower()

        return ''

    def _validate_table_access(self, table: str) -> None:
        """Ensure table is a subjective table."""
        if not table:
            raise ScopingViolation(f"Could not extract table name from SQL")

        if table not in SUBJECTIVE_TABLES:
            raise ScopingViolation(
                f"SafeDB can only access subjective tables. "
                f"Table '{table}' is not in SUBJECTIVE_TABLES. "
                f"Use create_unsafe_db() for device tables."
            )

    def _validate_query_scoping(self, sql: str) -> None:
        """Ensure query includes recorded_by filter."""
        sql_lower = sql.lower()
        if 'recorded_by' not in sql_lower and 'can_share_peer_id' not in sql_lower:
            raise ScopingViolation(
                f"Query on subjective table must include recorded_by or can_share_peer_id filter.\n"
                f"SQL: {sql}"
            )

    def _validate_returned_rows(self, results: list[dict[str, Any]], sql: str) -> None:
        """Ensure all returned rows match the expected recorded_by."""
        for row in results:
            # Check recorded_by
            row_recorded_by = row.get('recorded_by')
            if row_recorded_by is not None and row_recorded_by != self.recorded_by:
                raise ScopingViolation(
                    f"SCOPING VIOLATION: Query returned row with recorded_by={row_recorded_by}, "
                    f"expected {self.recorded_by}\n"
                    f"SQL: {sql}\n"
                    f"This indicates a bug in your WHERE clause."
                )

            # Check can_share_peer_id (used by shareable_events)
            can_share = row.get('can_share_peer_id')
            if can_share is not None and can_share != self.recorded_by:
                raise ScopingViolation(
                    f"SCOPING VIOLATION: Query returned row with can_share_peer_id={can_share}, "
                    f"expected {self.recorded_by}\n"
                    f"SQL: {sql}\n"
                    f"This indicates a bug in your WHERE clause."
                )

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute query on subjective table with scoping validation."""
        table = self._extract_table(sql)
        self._validate_table_access(table)
        self._validate_query_scoping(sql)

        results = self._db.query(sql, params)
        self._validate_returned_rows(results, sql)
        return results

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Alias for query()."""
        return self.query(sql, params)

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """Execute query and return single row with scoping validation."""
        table = self._extract_table(sql)
        self._validate_table_access(table)
        self._validate_query_scoping(sql)

        result = self._db.query_one(sql, params)
        if result is not None:
            self._validate_returned_rows([result], sql)
        return result

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute INSERT/UPDATE/DELETE on subjective table."""
        table = self._extract_table(sql)
        self._validate_table_access(table)

        # For INSERT/UPDATE/DELETE, verify recorded_by is in the operation
        sql_upper = sql.upper()
        if 'INSERT' in sql_upper:
            # Check that params likely includes recorded_by value
            if self.recorded_by not in str(params):
                raise ScopingViolation(
                    f"INSERT into subjective table must include recorded_by={self.recorded_by}\n"
                    f"SQL: {sql}\n"
                    f"Params: {params}"
                )
        elif 'UPDATE' in sql_upper or 'DELETE' in sql_upper:
            # Check that WHERE clause includes recorded_by
            if 'recorded_by' not in sql.lower() and 'can_share_peer_id' not in sql.lower():
                raise ScopingViolation(
                    f"UPDATE/DELETE on subjective table must filter by recorded_by\n"
                    f"SQL: {sql}"
                )

        self._db.execute(sql, params)

    def execute_returning(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute with RETURNING clause and validate results."""
        table = self._extract_table(sql)
        self._validate_table_access(table)

        results = self._db.execute_returning(sql, params)
        self._validate_returned_rows(results, sql)
        return results

    def get_blob(self, event_id: str) -> bytes:
        """Get blob from store only if this peer has validated this event.

        Joins store table with valid_events to enforce scoping.

        Args:
            event_id: The event ID (base64 encoded string)

        Returns:
            The blob bytes

        Raises:
            ScopingViolation: If this peer doesn't have access to this event
        """
        # Join store with valid_events to verify access
        # Note: store.id is TEXT (base64), not BLOB
        result = self._db.query_one("""
            SELECT s.blob
            FROM store s
            INNER JOIN valid_events v
              ON s.id = v.event_id AND v.recorded_by = ?
            WHERE s.id = ?
        """, (self.recorded_by, event_id))

        if result is None:
            raise ScopingViolation(
                f"SCOPING VIOLATION: Peer {self.recorded_by} doesn't have access to event {event_id}\n"
                f"Event not found in valid_events for this peer."
            )

        return result['blob']

    def get_shareable_blob(self, event_id: str) -> bytes:
        """Get blob from store only if this peer can share this event.

        Joins store table with shareable_events to enforce scoping for sync.
        This is less restrictive than get_blob() - allows sharing encrypted events.

        Args:
            event_id: The event ID (base64 encoded string)

        Returns:
            The blob bytes

        Raises:
            ScopingViolation: If this peer cannot share this event
        """
        # Join store with shareable_events to verify can share
        # Note: store.id is TEXT (base64), not BLOB
        result = self._db.query_one("""
            SELECT s.blob
            FROM store s
            INNER JOIN shareable_events se
              ON se.event_id = ? AND se.can_share_peer_id = ?
            WHERE s.id = ?
        """, (event_id, self.recorded_by, event_id))

        if result is None:
            raise ScopingViolation(
                f"SCOPING VIOLATION: Peer {self.recorded_by} cannot share event {event_id}\n"
                f"Event not found in shareable_events for this peer."
            )

        return result['blob']

    def commit(self) -> None:
        """Commit the current transaction."""
        self._db.commit()

    def changes(self) -> int:
        """Return number of rows affected by last execute()."""
        return self._db.changes()


class UnsafeDB:
    """Database wrapper for device-wide tables (not scoped by recorded_by).

    Usage:
        unsafedb = create_unsafe_db(db)
        peers = unsafedb.query("SELECT * FROM local_peers")
        unsafedb.execute("INSERT INTO store VALUES (...)", (...))

    All operations on device tables are validated to ensure:
    1. The table is in DEVICE_TABLES (not a subjective table)
    """

    def __init__(self, db: Database):
        self._db = db

    def _extract_table(self, sql: str) -> str:
        """Extract table name from SQL statement."""
        sql_upper = sql.upper().strip()

        # Handle SELECT
        if 'FROM' in sql_upper:
            match = re.search(r'\bFROM\s+(\w+)', sql_upper)
            if match:
                return match.group(1).lower()

        # Handle INSERT (including INSERT OR IGNORE, INSERT OR REPLACE)
        if 'INSERT' in sql_upper:
            match = re.search(r'\bINSERT\s+(?:OR\s+(?:IGNORE|REPLACE)\s+)?INTO\s+(\w+)', sql_upper)
            if match:
                return match.group(1).lower()

        # Handle UPDATE
        if 'UPDATE' in sql_upper:
            match = re.search(r'\bUPDATE\s+(\w+)', sql_upper)
            if match:
                return match.group(1).lower()

        # Handle DELETE
        if 'DELETE FROM' in sql_upper:
            match = re.search(r'\bDELETE\s+FROM\s+(\w+)', sql_upper)
            if match:
                return match.group(1).lower()

        return ''

    def _validate_table_access(self, table: str) -> None:
        """Ensure table is a device table, not a subjective table."""
        if not table:
            raise ScopingViolation(f"Could not extract table name from SQL")

        if table in SUBJECTIVE_TABLES:
            raise ScopingViolation(
                f"UnsafeDB cannot access subjective tables. "
                f"Table '{table}' requires recorded_by scoping. "
                f"Use create_safe_db(db, recorded_by=...) instead."
            )

        if table not in DEVICE_TABLES:
            raise ScopingViolation(
                f"Table '{table}' is not in DEVICE_TABLES. "
                f"Add it to DEVICE_TABLES in db.py if this is a device-wide table."
            )

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute query on device table."""
        table = self._extract_table(sql)
        self._validate_table_access(table)
        return self._db.query(sql, params)

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Alias for query()."""
        return self.query(sql, params)

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """Execute query and return single row."""
        table = self._extract_table(sql)
        self._validate_table_access(table)
        return self._db.query_one(sql, params)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute INSERT/UPDATE/DELETE on device table."""
        table = self._extract_table(sql)
        self._validate_table_access(table)
        self._db.execute(sql, params)

    def execute_returning(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute with RETURNING clause."""
        table = self._extract_table(sql)
        self._validate_table_access(table)
        return self._db.execute_returning(sql, params)

    def commit(self) -> None:
        """Commit the current transaction."""
        self._db.commit()

    def changes(self) -> int:
        """Return number of rows affected by last execute()."""
        return self._db.changes()


def create_safe_db(db: Database, recorded_by: str) -> SafeDB:
    """Create a SafeDB instance scoped to a specific peer.

    Args:
        db: The underlying Database instance
        recorded_by: The peer_id that owns this scoped view

    Returns:
        SafeDB instance that enforces scoping for all operations

    Example:
        safedb = create_safe_db(db, recorded_by='alice_peer_id')
        messages = safedb.query(
            "SELECT * FROM messages WHERE channel_id = ? AND recorded_by = ?",
            ('chan1', safedb.recorded_by)
        )
    """
    return SafeDB(db, recorded_by)


def create_unsafe_db(db: Database) -> UnsafeDB:
    """Create an UnsafeDB instance for device-wide operations.

    Args:
        db: The underlying Database instance

    Returns:
        UnsafeDB instance for accessing device-wide tables

    Example:
        unsafedb = create_unsafe_db(db)
        peers = unsafedb.query("SELECT * FROM local_peers")
    """
    return UnsafeDB(db)
