"""
Database setup and utilities with read-only support.
"""
import sqlite3
from typing import Any, Optional
import os
import glob


def _load_schema_file(schema_file: str, conn: sqlite3.Connection) -> None:
    """Load a schema file into the database."""
    with open(schema_file, 'r') as f:
        schema_sql = f.read()
        for statement in schema_sql.split(';'):
            statement = statement.strip()
            if statement:
                conn.execute(statement + ';')


def get_connection(db_path: str = "quiet.db") -> sqlite3.Connection:
    """Get a database connection with proper settings."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_database(conn: sqlite3.Connection, protocol_dir: Optional[str] = None) -> None:
    """Initialize database schema.

    The framework defines core tables and loads protocol-specific schema from:
    1. Handler-specific .sql files in handlers/
    2. Event type-specific .sql files in events/
    3. Any top-level .sql files in the protocol directory
    """

    # Load core framework schemas
    core_dir = os.path.dirname(os.path.abspath(__file__))
    for schema_file in glob.glob(os.path.join(core_dir, '*.sql')):
        _load_schema_file(schema_file, conn)

    if protocol_dir:
        
        # Load any top-level schema files in the protocol directory
        for schema_file in glob.glob(os.path.join(protocol_dir, '*.sql')):
            _load_schema_file(schema_file, conn)
        
        # Load event type schemas
        events_dir = os.path.join(protocol_dir, 'events')
        if os.path.exists(events_dir):
            # Look in subdirectories for event type schemas
            for event_type_dir in os.listdir(events_dir):
                event_type_path = os.path.join(events_dir, event_type_dir)
                if os.path.isdir(event_type_path):
                    for schema_file in glob.glob(os.path.join(event_type_path, '*.sql')):
                        _load_schema_file(schema_file, conn)

            # Also check for schemas directly in events/
            for schema_file in glob.glob(os.path.join(events_dir, '*.sql')):
                _load_schema_file(schema_file, conn)
        
        # Load handler schemas
        handlers_dir = os.path.join(protocol_dir, 'handlers')
        if os.path.exists(handlers_dir):
            # Look in subdirectories for handler schemas
            for handler_dir in os.listdir(handlers_dir):
                handler_path = os.path.join(handlers_dir, handler_dir)
                if os.path.isdir(handler_path):
                    for schema_file in glob.glob(os.path.join(handler_path, '*.sql')):
                        _load_schema_file(schema_file, conn)

            # Check for .sql files directly in handlers/
            for schema_file in glob.glob(os.path.join(handlers_dir, '*.sql')):
                _load_schema_file(schema_file, conn)
    
    conn.commit()


class ReadOnlyConnection:
    """
    A read-only wrapper around sqlite3.Connection that prevents modifications.
    """
    
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection
        self._conn.row_factory = sqlite3.Row
        
    def execute(self, sql: str, parameters: tuple = ()) -> sqlite3.Cursor:
        """Execute a SQL query (read-only)."""
        # Check if this is a modifying query
        sql_upper = sql.upper().strip()
        modifying_keywords = ['INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP', 'ALTER', 'REPLACE']
        
        for keyword in modifying_keywords:
            if sql_upper.startswith(keyword):
                raise PermissionError(f"Read-only connection cannot execute {keyword} statements")
        
        return self._conn.execute(sql, parameters)
    
    def executemany(self, sql: str, seq_of_parameters: Any) -> sqlite3.Cursor:
        """Execute many SQL queries (read-only)."""
        raise PermissionError("Read-only connection cannot execute multiple statements")
    
    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        """Execute a SQL script (read-only)."""
        raise PermissionError("Read-only connection cannot execute scripts")
    
    def commit(self) -> None:
        """Commit is a no-op for read-only connections."""
        pass
    
    def rollback(self) -> None:
        """Rollback is a no-op for read-only connections."""
        pass
    
    def close(self) -> None:
        """Close the underlying connection."""
        # Don't actually close - let the owner of the real connection handle that
        pass
    
    def cursor(self) -> sqlite3.Cursor:
        """Get a cursor from the underlying connection."""
        return self._conn.cursor()
    
    @property
    def row_factory(self) -> Any:
        """Get row factory."""
        return self._conn.row_factory
    
    @row_factory.setter
    def row_factory(self, factory: Any) -> None:
        """Set row factory."""
        self._conn.row_factory = factory


def get_readonly_connection(connection: sqlite3.Connection) -> ReadOnlyConnection:
    """
    Get a read-only wrapper around a database connection.
    
    Args:
        connection: The underlying SQLite connection
        
    Returns:
        A read-only wrapper that prevents modifications
    """
    return ReadOnlyConnection(connection)