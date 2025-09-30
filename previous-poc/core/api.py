"""
Protocol API client - protocol-agnostic client that uses OpenAPI spec.
"""

import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional, List
import sqlite3

from .pipeline import PipelineRunner
from .db import get_connection, init_database
from .jobs import JobScheduler


class API:
    """Protocol API client using OpenAPI spec for operation discovery."""
    
    def __init__(self, protocol_dir: Path, reset_db: bool = True, db_path: Optional[Path] = None, root_secret: Optional[str | bytes] = None):
        """
        Initialize API client.
        
        Args:
            protocol_dir: Protocol directory path containing openapi.yaml
            reset_db: Whether to reset database on init
            db_path: Custom database path (defaults to protocol_dir/demo.db)
        """
        self.protocol_dir = Path(protocol_dir)
        
        # Database path
        self.db_path = db_path if db_path else self.protocol_dir / "demo.db"
        
        # Reset database if requested
        if reset_db and self.db_path.exists():
            os.remove(self.db_path)
        
        # Initialize database with protocol schema
        db = get_connection(str(self.db_path))
        init_database(db, str(self.protocol_dir))
        db.close()
        
        # Initialize pipeline runner
        self.runner = PipelineRunner(
            db_path=str(self.db_path),
            verbose=False,
            root_secret=root_secret,
        )

        # Initialize job scheduler
        self.scheduler = JobScheduler(
            db_path=str(self.db_path),
            protocol_name=self.protocol_dir.name,
        )
        
        # Load OpenAPI spec if present (optional)
        openapi_path = self.protocol_dir / "openapi.yaml"
        self.openapi = {}
        self._specless = False
        if openapi_path.exists():
            with open(openapi_path, 'r') as f:
                self.openapi = yaml.safe_load(f)
            # Parse operations from OpenAPI spec
            self._parse_operations()
        else:
            # No spec: operate in discovery-only mode
            self._specless = True
            self.operations = {}

        # Discover and register implementations
        self._discover_implementations()

        # If running without a spec, synthesize operation map from registries
        if self._specless:
            self._synthesize_operations_from_registries()
        else:
            # Validate all operations have implementations
            self._validate_operations()
    
    def _parse_operations(self) -> None:
        """Parse operations from OpenAPI spec."""
        self.operations = {}
        
        # Parse paths from OpenAPI
        for path, methods in self.openapi.get('paths', {}).items():
            for method, spec in methods.items():
                if 'operationId' in spec:
                    operation_id = spec['operationId']
                    self.operations[operation_id] = {
                        'path': path,
                        'method': method,
                        'spec': spec
                    }
    
    def _validate_operations(self) -> None:
        """Validate that all operations have corresponding implementations."""

        missing_implementations = []

        for operation_id, operation in self.operations.items():
            # Check based on operation ID format
            if operation_id.startswith('core.'):
                # Core operations - verify they exist dynamically
                core_op = operation_id.replace('core.', 'core_')
                if not self._has_core_operation(core_op):
                    missing_implementations.append((operation_id, 'core_command'))

            elif operation['method'] == 'post':
                # Should be a command
                # Commands deprecated; ensure flow exists instead
                from core.flows import flows_registry
                if not flows_registry.has_flow(operation_id):
                    missing_implementations.append((operation_id, 'flow'))

            elif operation['method'] == 'get':
                # Should be a query
                if not self.query_registry.has_query(operation_id):
                    missing_implementations.append((operation_id, 'query'))

        if missing_implementations:
            error_msg = "Missing implementations for the following operation IDs:\n"
            for op_id, impl_type in missing_implementations:
                error_msg += f"  - {op_id} ({impl_type})\n"
            raise ValueError(error_msg)

    def _has_core_operation(self, core_op: str) -> bool:
        """Check if a core operation exists by trying to import it."""
        try:
            # Dynamically check for core operations
            from core import identity
            return hasattr(identity, core_op)
        except ImportError:
            return False

    def _discover_implementations(self) -> None:
        """Discover command and query implementations for operations."""
        import sys
        import importlib
        protocol_root = self.protocol_dir.parent.parent
        if str(protocol_root) not in sys.path:
            sys.path.insert(0, str(protocol_root))

        # Import registries
        from core.queries import query_registry
        from core.flows import flows_registry

        # Use the global query registry which has system queries
        self.query_registry = query_registry
        # Auto-discover protocol queries
        self.query_registry._auto_discover_queries(str(self.protocol_dir))

        # Prepare type index
        self.type_index: Dict[str, Dict[str, Any]] = {}

        # Discover commands from event directories
        events_dir = self.protocol_dir / "events"
        if events_dir.exists():
            for event_dir in events_dir.iterdir():
                if not event_dir.is_dir() or event_dir.name.startswith('_'):
                    continue

                event_type = event_dir.name

                # Commands removed: only import flows to register @flow_op operations
                # Also inspect queries for type metadata (registration handled by query_registry)
                queries_file = event_dir / 'queries.py'
                if queries_file.exists():
                    try:
                        q_module_name = f'protocols.{self.protocol_dir.name}.events.{event_type}.queries'
                        q_module = importlib.import_module(q_module_name)
                        for qname in dir(q_module):
                            qobj = getattr(q_module, qname)
                            if callable(qobj) and hasattr(qobj, '_is_query'):
                                op_id = f'{event_type}.{qname}'
                                param_t = getattr(qobj, '_param_type', None)
                                result_t = getattr(qobj, '_result_type', None)
                                if param_t is not None or result_t is not None:
                                    self.type_index[op_id] = {
                                        'params': param_t,
                                        'result': result_t
                                    }
                    except ImportError:
                        pass

                # Import flows to register @flow_op operations (if present)
                flows_file = event_dir / 'flows.py'
                if flows_file.exists():
                    try:
                        f_module_name = f'protocols.{self.protocol_dir.name}.events.{event_type}.flows'
                        importlib.import_module(f_module_name)
                    except ImportError:
                        pass

        # Load protocol-level API exposure map, if present
        self._api_exposed: dict[str, str] | None = None
        try:
            api_mod = importlib.import_module(f'protocols.{self.protocol_dir.name}.api')
            if hasattr(api_mod, 'EXPOSED') and isinstance(api_mod.EXPOSED, dict):
                # Normalize keys to strings
                self._api_exposed = {str(k): str(v) for k, v in api_mod.EXPOSED.items()}
            # Apply any flow aliases if provided
            if hasattr(api_mod, 'ALIASES') and isinstance(api_mod.ALIASES, dict):
                from core.flows import flows_registry as _flows_registry
                # Ensure flows are imported before aliasing
                for event_dir in (self.protocol_dir / 'events').iterdir():
                    if event_dir.is_dir() and (event_dir / 'flows.py').exists():
                        try:
                            importlib.import_module(f"protocols.{self.protocol_dir.name}.events.{event_dir.name}.flows")
                        except Exception:
                            pass
                for alias, target in api_mod.ALIASES.items():
                    try:
                        _flows_registry.alias(str(alias), str(target))
                    except Exception as e:
                        # Surface alias issues clearly
                        print(f"Warning: failed to alias flow '{alias}' -> '{target}': {e}")
        except ImportError:
            self._api_exposed = None

    def _synthesize_operations_from_registries(self) -> None:
        """When no OpenAPI spec is present, derive an operation map from registries.

        - Flows  => method post
        - Queries  => method get
        """
        from core.flows import flows_registry
        from core.queries import query_registry

        ops: Dict[str, Dict[str, object]] = {}

        for op in flows_registry.list_flows():
            ops[op] = {
                'path': f'/{op.split(".", 1)[0]}',
                'method': 'post',
                'spec': {}
            }

        for op in query_registry.list_queries():
            if op not in ops:
                ops[op] = {
                    'path': f'/{op.split(".", 1)[0]}s',
                    'method': 'get',
                    'spec': {}
                }

        self.operations = ops
    
    def execute_operation(self, operation_id: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Execute an operation by its OpenAPI operation ID."""
        # Universal identity requirement for non-system operations (protocol-specific)
        import importlib as _importlib
        api_mod = _importlib.import_module(f'protocols.{self.protocol_dir.name}.api')
        params = api_mod.normalize_actor(operation_id, params)
        # If protocol-level API exposure is defined, enforce it
        if hasattr(self, '_api_exposed') and self._api_exposed is not None:
            # Core operations always allowed
            if operation_id.startswith('core.'):
                core_op = operation_id.replace('core.', 'core_')
                return self._execute_core_command(core_op, params)

            if operation_id not in self._api_exposed:
                raise ValueError(f"Operation not exposed by API: {operation_id}")

            kind = self._api_exposed[operation_id]

            if kind == 'flow':
                from core.flows import flows_registry
                if not flows_registry.has_flow(operation_id):
                    raise ValueError(f"Flow not registered for: {operation_id}")
                db = get_connection(str(self.db_path))
                try:
                    import uuid
                    request_id = str(uuid.uuid4())
                    enriched_params: Dict[str, Any] = dict(params or {})
                    enriched_params['_db'] = db
                    enriched_params['_runner'] = self.runner
                    enriched_params['_protocol_dir'] = str(self.protocol_dir)
                    enriched_params['_request_id'] = request_id
                    return flows_registry.execute(operation_id, enriched_params)
                finally:
                    db.close()
            elif kind == 'command':
                return self._execute_command(operation_id, params)
            elif kind == 'query':
                return self._execute_query(operation_id, params)
            else:
                raise ValueError(f"Unknown EXPOSED kind for {operation_id}: {kind}")

        # No protocol-level API exposure: prefer flow ops if registered
        try:
            from core.flows import flows_registry
            if flows_registry.has_flow(operation_id):
                db = get_connection(str(self.db_path))
                try:
                    import uuid
                    request_id = str(uuid.uuid4())
                    enriched_params: Dict[str, Any] = dict(params or {})
                    enriched_params['_db'] = db
                    enriched_params['_runner'] = self.runner
                    enriched_params['_protocol_dir'] = str(self.protocol_dir)
                    enriched_params['_request_id'] = request_id
                    return flows_registry.execute(operation_id, enriched_params)
                finally:
                    db.close()
        except Exception:
            pass
        # Check if this starts with 'core.' for core operations
        if operation_id.startswith('core.'):
            # Map to internal core function names
            core_op = operation_id.replace('core.', 'core_')
            return self._execute_core_command(core_op, params)

        # In specless mode or if operation not parsed from spec, fall back to flow/query registries
        if operation_id not in self.operations:
            from core.queries import query_registry
            from core.flows import flows_registry
            if flows_registry.has_flow(operation_id):
                db = get_connection(str(self.db_path))
                try:
                    import uuid
                    request_id = str(uuid.uuid4())
                    enriched_params: Dict[str, Any] = dict(params or {})
                    enriched_params['_db'] = db
                    enriched_params['_runner'] = self.runner
                    enriched_params['_protocol_dir'] = str(self.protocol_dir)
                    enriched_params['_request_id'] = request_id
                    return flows_registry.execute(operation_id, enriched_params)
                finally:
                    db.close()
            if query_registry.has_query(operation_id):
                return self._execute_query(operation_id, params)
            raise ValueError(f"Unknown operation: {operation_id}")

        operation = self.operations[operation_id]

        if operation['method'] == 'post':
            # Execute as flow (commands removed)
            from core.flows import flows_registry
            db = get_connection(str(self.db_path))
            try:
                import uuid
                request_id = str(uuid.uuid4())
                enriched_params: Dict[str, Any] = dict(params or {})
                enriched_params['_db'] = db
                enriched_params['_runner'] = self.runner
                enriched_params['_protocol_dir'] = str(self.protocol_dir)
                enriched_params['_request_id'] = request_id
                return flows_registry.execute(operation_id, enriched_params)
            finally:
                db.close()
        elif operation['method'] == 'get':
            # Execute as query
            return self._execute_query(operation_id, params)
        else:
            raise ValueError(f"Unsupported method: {operation['method']}")
    
    def _execute_command(self, operation_id: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Commands deprecated; flows handle operations."""
        raise ValueError(f"Commands are deprecated. Use flows for operation: {operation_id}")

    def _execute_core_command(self, operation_id: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute a core framework command (not through pipeline)."""
        # Dynamically import and execute core commands
        try:
            from core import identity

            if hasattr(identity, operation_id):
                from typing import Dict as _Dict, Any as _Any, Callable as _Callable, cast as _cast
                func = getattr(identity, operation_id)
                # Execute the core command directly with db_path
                return _cast(_Dict[str, _Any], func(params or {}, str(self.db_path)))
            else:
                raise ValueError(f"Unknown core command: {operation_id}")

        except ImportError:
            raise ValueError(f"Core module not available for: {operation_id}")

    def _execute_query(self, operation_id: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Execute a query."""
        # Get database connection
        db = get_connection(str(self.db_path))

        try:
            # Operation IDs for queries already use the event_type.function format
            # Just execute directly through the registry
            result = self.query_registry.execute(operation_id, params or {}, db)
            return result

        finally:
            db.close()

    def tick_scheduler(self) -> int:
        """
        Check for due jobs and execute their operations directly.

        Returns:
            Number of jobs triggered
        """
        due_jobs = self.scheduler.tick()
        for job in due_jobs:
            try:
                self.execute_operation(job['op'], job.get('params', {}))
            except Exception as e:
                print(f"[Scheduler] Job {job['op']} failed: {e}")
        return len(due_jobs)
    
    def __getattr__(self, name: str) -> Any:
        """Dynamic method creation for OpenAPI operations."""
        # Check if this is an operation from OpenAPI spec
        if name in self.operations:
            def operation_method(params: Optional[Dict[str, Any]] = None) -> Any:
                # Always expect a params dict
                return self.execute_operation(name, params)
            return operation_method
        
        # If not found, raise AttributeError
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    def dump_database(self, limit_per_table: int | None = None) -> dict[str, list[dict[str, Any]]]:
        """
        Return a raw dump of key tables for inspection.

        Args:
            limit_per_table: Optional cap on number of rows returned per table.

        Returns:
            Mapping of table name -> list of row dicts.
        """
        import sqlite3 as _sqlite3

        tables_to_dump = [
            "peers",
            "users",
            "groups",
            "group_members",
            "channels",
            "messages",
            "events",
            "projected_events",
            "blocked_events",
        ]

        result: dict[str, list[dict[str, Any]]] = {}

        conn = get_connection(str(self.db_path))
        try:
            conn.row_factory = _sqlite3.Row
            cur = conn.cursor()

            for table in tables_to_dump:
                # Check table exists
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                    (table,)
                )
                if cur.fetchone() is None:
                    result[table] = []
                    continue

                # Special ordering for certain tables
                order_by = None
                if table == "events":
                    # event store recency
                    order_by = "received_at DESC"
                elif table == "messages":
                    order_by = "created_at DESC"
                elif table in ("groups", "channels", "users", "peers"):
                    # best-effort chronological ordering if timestamp column exists
                    # will fallback below if column missing
                    order_by = "created_at DESC"

                query = f"SELECT * FROM {table}"
                if order_by:
                    # Verify the column exists before adding ORDER BY
                    try:
                        cur.execute(f"PRAGMA table_info({table})")
                        cols = {r[1] for r in cur.fetchall()}
                        if any(col in cols for col in [c.strip().split()[0] for c in order_by.split(',')]):
                            query += f" ORDER BY {order_by}"
                    except Exception:
                        pass

                if limit_per_table is not None and isinstance(limit_per_table, int) and limit_per_table > 0:
                    query += f" LIMIT {limit_per_table}"

                cur.execute(query)
                rows = cur.fetchall()
                result[table] = [dict(r) for r in rows]

        finally:
            conn.close()

        return result


# Backward-compatible alias (tests/imports may still use APIClient)
APIClient = API

class APIError(Exception):
    """API error with status code."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"API Error {status_code}: {message}")
