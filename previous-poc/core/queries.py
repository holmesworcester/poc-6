"""
Generic query registry system for protocols.
Queries are registered dynamically and enforce read-only database access.
"""
from typing import Dict, Any, List, Callable, Optional, TypeVar
import sqlite3
import functools
import inspect
import importlib
from pathlib import Path
from .db import ReadOnlyConnection, get_readonly_connection

T = TypeVar('T')


class QueryRegistry:
    """Registry for protocol queries with read-only enforcement."""

    def __init__(self, protocol_dir: Optional[str] = None):
        self._queries: Dict[str, Callable] = {}
        if protocol_dir:
            self._auto_discover_queries(protocol_dir)

    def register(self, name: str, query: Callable) -> None:
        """Register a query function."""
        self._queries[name] = query

    def execute(self, name: str, params: Dict[str, Any], db: sqlite3.Connection) -> Any:
        """Execute a query with read-only database access.

        Actor normalization is enforced at the API boundary. This registry
        simply executes registered queries with a read-only connection.
        """
        if name not in self._queries:
            raise ValueError(f"Unknown query: {name}")

        query = self._queries[name]

        # Wrap connection in read-only wrapper
        readonly_db = get_readonly_connection(db)

        # Execute query with read-only connection
        return query(readonly_db, params)

    def list_queries(self) -> List[str]:
        """Return list of registered query names."""
        return sorted(self._queries.keys())

    def has_query(self, name: str) -> bool:
        """Check if a query is registered."""
        return name in self._queries

    def _auto_discover_queries(self, protocol_dir: str) -> None:
        """Auto-discover and register queries from protocol event modules."""
        protocol_path = Path(protocol_dir)
        events_dir = protocol_path / 'events'

        if not events_dir.exists():
            return

        # Find all event type directories
        for event_dir in events_dir.iterdir():
            if not event_dir.is_dir() or event_dir.name.startswith('_'):
                continue

            event_type = event_dir.name
            queries_file = event_dir / 'queries.py'

            if queries_file.exists():
                try:
                    # Import the queries module
                    module_name = f'protocols.{protocol_path.name}.events.{event_type}.queries'
                    module = importlib.import_module(module_name)

                    # Find all functions decorated with @query
                    for name, obj in inspect.getmembers(module):
                        if callable(obj) and hasattr(obj, '_is_query'):
                            # Register with event_type.function_name format
                            query_name = f'{event_type}.{name}'
                            self.register(query_name, obj)
                except ImportError:
                    # Skip if module can't be imported
                    pass


# Global query registry (will be initialized with protocol_dir when needed)
query_registry = QueryRegistry()


def query(_func: Callable | None = None, *, param_type: Any | None = None, result_type: Any | None = None) -> Callable:
    """
    Decorator for query functions that enforces read-only database access.

    Usage:
      @query
      def get(db, params): ...

      @query(param_type=GetParams, result_type=list[RowType])
      def get(db, params): ...

    Query functions receive a ReadOnlyConnection that prevents modifications.
    """
    def _decorate(func: Callable) -> Callable:
        # Check signature - standard is (db, params)
        sig = inspect.signature(func)
        param_names = list(sig.parameters.keys())

        if not param_names or param_names[0] != 'db':
            raise TypeError(
                f"{func.__name__} must have 'db' as first parameter for database connection"
            )

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # db is first argument - wrap it in read-only if needed
            if args and isinstance(args[0], sqlite3.Connection):
                readonly_conn = get_readonly_connection(args[0])
                args = (readonly_conn,) + args[1:]
            elif args and not isinstance(args[0], ReadOnlyConnection):
                raise TypeError(
                    f"{func.__name__} must receive a database connection as first argument"
                )

            return func(*args, **kwargs)

        # Mark as query function
        wrapper._is_query = True  # type: ignore[attr-defined]
        # Optional type metadata
        if param_type is not None:
            wrapper._param_type = param_type  # type: ignore[attr-defined]
        if result_type is not None:
            wrapper._result_type = result_type  # type: ignore[attr-defined]

        # Don't auto-register here - let the API do it with proper operation IDs

        return wrapper

    # Support bare @query and @query(...)
    if _func is None:
        return _decorate
    else:
        return _decorate(_func)


# System query functions

def dump_database(db: ReadOnlyConnection, params: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Dump all tables in the database."""
    # Get all tables
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]

    result = {}
    for table in tables:
        cursor = db.execute(f"SELECT * FROM {table}")
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        table_results = []
        for row in rows:
            row_dict = dict(zip(columns, row))
            # Convert bytes to hex
            for key, value in row_dict.items():
                if isinstance(value, bytes):
                    row_dict[key] = value.hex()
            table_results.append(row_dict)
        result[table] = table_results
    return result


def get_logs(params: Dict[str, Any], db: ReadOnlyConnection) -> List[Dict[str, Any]]:
    """Get processor logs (placeholder for now)."""
    limit = params.get('limit', 100)
    # In the future, this could read from a logs table
    return []


# System queries are registered separately (not auto-discovered)
query_registry.register('system.dump_database', dump_database)
query_registry.register('system.logs', get_logs)
