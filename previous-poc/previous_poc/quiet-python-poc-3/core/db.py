"""
SQLite-backed dict with optional protocol schema support and simple transactions.

Goals:
- Keep a plain-dict interface for tests/handlers (get/set/del, iteration).
- Persist data to SQLite across runs.
- Allow loading protocol schema.sql files (with inline INDEX lines handled).
- Provide optional begin/commit/rollback and a simple with_retry helper.
"""
import json
import os
import sqlite3
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Iterator, Dict

class PersistentDict(MutableMapping):
    """Dict-like storage persisted in SQLite with optional transactions."""
    
    def __init__(self, db_path=":memory:", protocol_name=None):
        """
        Initialize persistent dict.
        
        Args:
            db_path: Path to SQLite database file. Defaults to in-memory.
            protocol_name: Name of protocol to load schema.sql from (optional).
        """
        self.db_path = db_path
        self.protocol_name = protocol_name
        # Use timeout to avoid database locked errors
        self.conn = sqlite3.connect(db_path, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        
        # SQLite pragmas for concurrency + durability
        try:
            # WAL allows readers during writes and improves concurrency
            self.conn.execute("PRAGMA journal_mode = WAL")
            # NORMAL is a good balance for local apps
            self.conn.execute("PRAGMA synchronous = NORMAL")
            # Enforce transactional safeguards
            self.conn.execute("PRAGMA foreign_keys = ON")
            # Default busy timeout; can be overridden per-operation
            self.conn.execute("PRAGMA busy_timeout = 30000")
            # Favor safer reads
            self.conn.execute("PRAGMA read_uncommitted = 0")
        except sqlite3.OperationalError:
            pass
        
        # Transaction state
        self._in_transaction = False
        self._transaction_cache = {}
        
        # Initialize with protocol schema if available
        if protocol_name:
            self._init_from_protocol_schema()
        else:
            self._init_default_tables()
        
        # Cache for better performance
        self._cache = {}
        self._load_cache()
    
    def _init_from_protocol_schema(self):
        """Initialize database using a protocol's schema.sql if available."""
        protocol_path = Path("protocols") / self.protocol_name
        schema_path = protocol_path / "schema.sql"
        
        if schema_path.exists():
            # Read and process the schema
            with open(schema_path, 'r') as f:
                schema_sql = f.read()
            
            # Extract inline INDEX lines from CREATE TABLE blocks into separate CREATE INDEX
            lines = schema_sql.split('\n')
            cleaned_lines: list[str] = []
            indexes_to_create: list[str] = []
            in_create_table = False
            table_name: str | None = None
            
            for line in lines:
                stripped = line.strip()
                if stripped.upper().startswith('CREATE TABLE'):
                    in_create_table = True
                    import re
                    match = re.search(r'CREATE TABLE IF NOT EXISTS (\w+)', line, re.IGNORECASE)
                    if match:
                        table_name = match.group(1)
                    cleaned_lines.append(line)
                elif in_create_table and stripped.upper().startswith('INDEX '):
                    # INDEX idx_name (col) -> CREATE INDEX idx_name ON <table> (col)
                    parts = stripped.split()
                    if len(parts) >= 3 and table_name:
                        idx_name = parts[1]
                        start = stripped.find('(')
                        if start != -1:
                            columns = stripped[start:]
                            columns = columns.rstrip(',')
                            indexes_to_create.append(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table_name} {columns};")
                    continue
                elif in_create_table and stripped.startswith('--'):
                    continue
                elif in_create_table and (');' in stripped or stripped == ')'):  
                    in_create_table = False
                    if cleaned_lines:
                        prev = cleaned_lines[-1]
                        try:
                            comment_idx = prev.find('--')
                            if comment_idx != -1:
                                code_part = prev[:comment_idx].rstrip()
                                comment_part = prev[comment_idx:]
                            else:
                                code_part = prev.rstrip()
                                comment_part = ''
                            if code_part.endswith(','):
                                code_part = code_part[:-1]
                            # rebuild, keep a space before comment if needed
                            new_prev = code_part + ((' ' + comment_part) if comment_part else '')
                            cleaned_lines[-1] = new_prev
                        except Exception:
                            # fallback to previous behavior
                            if cleaned_lines[-1].rstrip().endswith(','):
                                cleaned_lines[-1] = cleaned_lines[-1].rstrip()[:-1]
                    cleaned_lines.append(line)
                else:
                    cleaned_lines.append(line)
            
            cleaned_sql = '\n'.join(cleaned_lines)
            # Extra safety: remove any stray trailing commas before closing parens in CREATE TABLE blocks
            try:
                import re as _re
                cleaned_sql = _re.sub(r",\s*\)", ")", cleaned_sql)
            except Exception:
                pass
            
            # Execute the cleaned schema
            cursor = self.conn.cursor()
            try:
                cursor.executescript(cleaned_sql)
            except sqlite3.OperationalError as e:
                try:
                    with open('.last_cleaned_schema.sql', 'w') as _f:
                        _f.write(cleaned_sql)
                except Exception:
                    pass
                raise
            
            # Create indexes separately
            for idx_sql in indexes_to_create:
                try:
                    cursor.execute(idx_sql)
                except sqlite3.OperationalError:
                    pass
            
            self.conn.commit()
            
            # Also create our generic tables for compatibility
            self._init_default_tables()
        else:
            # Fall back to default tables
            self._init_default_tables()
    
    def _init_default_tables(self):
        """Initialize generic storage tables used by the dict facade."""
        cursor = self.conn.cursor()
        
        # Key-value store for dict and primitives
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS _kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        
        # Event store mirror for convenience
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS _event_store (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_data TEXT NOT NULL
            )
        """)
        
        # List storage with stable ordering
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS _list_store (
                list_name TEXT NOT NULL,
                item_order INTEGER NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (list_name, item_order)
            )
        """)
        
        self.conn.commit()
    
    def _load_cache(self):
        """Load all persisted data into the in-memory cache."""
        cursor = self.conn.cursor()
        
        self._cache = {}
        
        # Load all key-value data
        try:
            for row in cursor.execute("SELECT key, value FROM _kv_store"):
                key = row['key']
                value = json.loads(row['value'])
                self._cache[key] = value
        except sqlite3.OperationalError:
            pass  # table might not exist yet
        
        # Load all list data
        try:
            list_data = {}
            for row in cursor.execute("SELECT list_name, item_order, data FROM _list_store ORDER BY list_name, item_order"):
                list_name = row['list_name']
                if list_name not in list_data:
                    list_data[list_name] = []
                list_data[list_name].append(json.loads(row['data']))
            
            for list_name, items in list_data.items():
                self._cache[list_name] = items
        except sqlite3.OperationalError:
            pass
        
        # Load event store if it exists
        try:
            events = []
            for row in cursor.execute("SELECT event_data FROM _event_store ORDER BY id"):
                events.append(json.loads(row['event_data']))
            if events:
                self._cache['eventStore'] = events
        except sqlite3.OperationalError:
            pass
    
    def __getitem__(self, key):
        """Get item from dict"""
        # Check transaction cache first
        if self._in_transaction and key in self._transaction_cache:
            return self._transaction_cache[key]
        if key not in self._cache:
            raise KeyError(key)
        return self._cache[key]
    
    def __setitem__(self, key, value):
        """Set item and persist immediately, or stage if in a transaction."""
        if self._in_transaction:
            self._transaction_cache[key] = value
        else:
            self._cache[key] = value
            self._persist_key(key, value)
    
    def __delitem__(self, key):
        """Delete item from dict"""
        del self._cache[key]
        self._delete_key(key)
    
    def __iter__(self):
        """Iterate over keys including staged transaction keys when active."""
        if self._in_transaction:
            return iter(set(self._cache).union(self._transaction_cache))
        return iter(self._cache)
    
    def __len__(self):
        """Return number of keys"""
        return len(self._cache)
    
    def __contains__(self, key):
        """Check if key exists"""
        if self._in_transaction and key in self._transaction_cache:
            return True
        return key in self._cache
    
    def begin_transaction(self):
        """Start a new transaction and take an exclusive write lock."""
        if self._in_transaction:
            raise RuntimeError("Transaction already in progress")
        self._in_transaction = True
        self._transaction_cache = {}
        # Use IMMEDIATE to acquire a reserved lock for writes while allowing readers
        self.conn.execute("BEGIN IMMEDIATE")
    
    def commit(self):
        """Commit staged changes and clear transaction state."""
        if not self._in_transaction:
            return
            
        try:
            for key, value in self._transaction_cache.items():
                self._cache[key] = value
                self._persist_key(key, value)
            
            self.conn.commit()
        finally:
            self._transaction_cache = {}
            self._in_transaction = False
    
    def rollback(self):
        """Discard staged changes and clear transaction state."""
        if not self._in_transaction:
            return
            
        try:
            self.conn.rollback()
        finally:
            self._transaction_cache = {}
            self._in_transaction = False
    
    def with_retry(self, func, max_retries=3, timeout_ms=30000):
        """Execute a function with a busy-timeout and simple retry on locks."""
        for attempt in range(max_retries):
            try:
                self.conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
                return func()
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))  # Exponential backoff
                    continue
                raise
    
    def _persist_key(self, key, value):
        """Persist a key/value to the backing tables based on type."""
        cursor = self.conn.cursor()
        
        if isinstance(value, dict):
            cursor.execute("""
                INSERT OR REPLACE INTO _kv_store (key, value)
                VALUES (?, ?)
            """, (key, json.dumps(value)))
        
        elif isinstance(value, list):
            cursor.execute("DELETE FROM _list_store WHERE list_name = ?", (key,))
            
            for idx, item in enumerate(value):
                cursor.execute("""
                    INSERT INTO _list_store (list_name, item_order, data)
                    VALUES (?, ?, ?)
                """, (key, idx, json.dumps(item)))
            
            # Persist empty lists in _kv_store so they survive reloads
            if len(value) == 0:
                cursor.execute(
                    "INSERT OR REPLACE INTO _kv_store (key, value) VALUES (?, ?)",
                    (key, json.dumps([]))
                )
            else:
                # Remove any stale empty marker when list has items
                cursor.execute("DELETE FROM _kv_store WHERE key = ?", (key,))
            
            # Mirror to _event_store for compatibility
            if key == 'eventStore':
                cursor.execute("DELETE FROM _event_store")
                for event in value:
                    cursor.execute("""
                        INSERT INTO _event_store (event_data)
                        VALUES (?)
                    """, (json.dumps(event),))
        
        else:
            cursor.execute("""
                INSERT OR REPLACE INTO _kv_store (key, value)
                VALUES (?, ?)
            """, (key, json.dumps(value)))
        
        self.conn.commit()
    
    def _delete_key(self, key):
        """Delete a key from all backing tables."""
        cursor = self.conn.cursor()
        
        cursor.execute("DELETE FROM _kv_store WHERE key = ?", (key,))
        cursor.execute("DELETE FROM _list_store WHERE list_name = ?", (key,))
        if key == 'eventStore':
            cursor.execute("DELETE FROM _event_store")
        
        self.conn.commit()
    
    def clear(self):
        """Clear generic tables and in-memory cache."""
        cursor = self.conn.cursor()
        
        cursor.execute("DELETE FROM _kv_store")
        cursor.execute("DELETE FROM _event_store")
        cursor.execute("DELETE FROM _list_store")
        
        self.conn.commit()
        self._cache.clear()
    
    def update(self, other):
        """Update dict with another dict"""
        for key, value in other.items():
            self[key] = value
    
    def to_dict(self):
        """Return a shallow copy of the in-memory view as a plain dict."""
        return dict(self._cache)
    
    def get(self, key, default=None):
        """Get item with default value"""
        try:
            return self[key]
        except KeyError:
            return default
    
    def update_nested(self, key, updater_func):
        """Apply `updater_func` to a nested value and persist the result."""
        value = self.get(key, {})
        updater_func(value)
        self[key] = value  # Trigger persistence
        return value
    
    def snapshot(self):
        """
        Create a snapshot of the database state.
        
        Returns a dictionary containing:
        - schema: The SQL schema of all tables
        - data: A dictionary of table names to their data
        """
        snapshot = {
            'schema': {},
            'data': {}
        }
        
        if not self.conn:
            return snapshot
            
        try:
            cursor = self.conn.cursor()
            
            # Get all table names (excluding internal SQLite tables)
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """)
            tables = [row[0] for row in cursor.fetchall()]
            
            # Get schema for each table
            for table in tables:
                cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
                result = cursor.fetchone()
                if result:
                    snapshot['schema'][table] = result[0]
                
                # Also get indices
                cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))
                indices = cursor.fetchall()
                if indices:
                    snapshot['schema'][f'{table}_indices'] = [idx[0] for idx in indices]
            
            # Get data for each table
            for table in tables:
                cursor.execute(f"SELECT * FROM {table}")
                columns = [description[0] for description in cursor.description]
                rows = []
                for row in cursor.fetchall():
                    row_dict = {columns[i]: row[i] for i in range(len(columns))}
                    rows.append(row_dict)
                snapshot['data'][table] = {
                    'columns': columns,
                    'rows': rows
                }
                
        except Exception as e:
            snapshot['error'] = str(e)
            
        return snapshot
    
    def snapshot_to_sql(self):
        """
        Create a SQL dump of the database that can be executed to recreate it.
        
        Returns a string containing SQL statements.
        """
        if not self.conn:
            return ""
            
        sql_statements = []
        
        try:
            cursor = self.conn.cursor()
            
            # Get all table names (excluding internal SQLite tables)
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """)
            tables = [row[0] for row in cursor.fetchall()]
            
            # Add schema creation statements
            for table in tables:
                cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
                result = cursor.fetchone()
                if result:
                    sql_statements.append(result[0] + ";")
                    
            # Add index creation statements
            cursor.execute("""
                SELECT sql FROM sqlite_master 
                WHERE type='index' AND sql IS NOT NULL
                ORDER BY name
            """)
            for row in cursor.fetchall():
                sql_statements.append(row[0] + ";")
                
            # Add data insertion statements
            for table in tables:
                cursor.execute(f"SELECT * FROM {table}")
                columns = [description[0] for description in cursor.description]
                
                for row in cursor.fetchall():
                    values = []
                    for val in row:
                        if val is None:
                            values.append("NULL")
                        elif isinstance(val, str):
                            # Escape single quotes
                            escaped = val.replace("'", "''")
                            values.append(f"'{escaped}'")
                        else:
                            values.append(str(val))
                    
                    insert_stmt = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(values)});"
                    sql_statements.append(insert_stmt)
                    
        except Exception as e:
            sql_statements.append(f"-- Error: {e}")
            
        return "\n".join(sql_statements)
    
    def close(self):
        """Close the SQLite connection if open."""
        if hasattr(self, 'conn') and self.conn:
            self.conn.close()
            self.conn = None
    
    def __del__(self):
        """Ensure connection is closed when object is garbage collected"""
        self.close()

def create_db(db_path=None, protocol_name=None):
    """Create a `PersistentDict`, inferring protocol from HANDLER_PATH if absent."""
    if db_path is None:
        db_path = os.environ.get('DB_PATH', ':memory:')
    
    if protocol_name is None and 'HANDLER_PATH' in os.environ:
        handler_path = Path(os.environ['HANDLER_PATH'])
        parts = handler_path.parts
        for i, part in enumerate(parts):
            if part == 'protocols' and i + 1 < len(parts):
                protocol_name = parts[i + 1]
                break
    
    return PersistentDict(db_path, protocol_name)
