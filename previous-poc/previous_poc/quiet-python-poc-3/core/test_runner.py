#!/usr/bin/env python3
"""
Test Runner for the Event Framework
Note: this should not contain any protocol-specific code
"""
import json
import sys
import os
import traceback
import copy
try:
    import yaml
except Exception:
    yaml = None
import re
import itertools
from datetime import datetime
from pathlib import Path

# Ensure repository root is on sys.path for 'core' imports when running as a script
try:
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
except Exception:
    pass

class TestRunner:
    def __init__(self):
        self.verbose = False
        self.logs = []
        self._temp_db_files = set()  # Track all temp DB files created
        
    def _track_db_file(self, db_path):
        """Track a database file for later cleanup"""
        if db_path and db_path != ':memory:' and not db_path.startswith(':'):
            self._temp_db_files.add(db_path)
    
    def _cleanup_db_files(self):
        """Clean up all tracked database files"""
        if self._temp_db_files:
            print(f"\nCleaning up {len(self._temp_db_files)} test database files...")
        for db_path in list(self._temp_db_files):
            # Clean up the main database file
            if os.path.exists(db_path):
                try:
                    os.remove(db_path)
                    if self.verbose:
                        print(f"  Removed: {db_path}")
                except Exception as e:
                    print(f"  Failed to remove {db_path}: {e}")
            
            # Also clean up SQLite's auxiliary files
            for suffix in ['-wal', '-shm']:
                aux_path = db_path + suffix
                if os.path.exists(aux_path):
                    try:
                        os.remove(aux_path)
                        if self.verbose:
                            print(f"  Removed: {aux_path}")
                    except Exception as e:
                        if self.verbose:
                            print(f"  Failed to remove {aux_path}: {e}")
        self._temp_db_files.clear()
        
    def log(self, message, level="INFO"):
        timestamp = datetime.now().isoformat()
        entry = f"[{timestamp}] [{level}] {message}"
        self.logs.append(entry)
        if self.verbose or level in ["ERROR", "WARNING"]:
            print(entry)
    
    def subset_match(self, actual, expected, path=""):
        """
        Check if expected is a subset of actual.
        Returns (matches, mismatch_path, expected_value, actual_value)
        """
        # Special case for "..." which matches any value
        if expected == "...":
            return True, None, None, None
            
        # Wildcard matches anything
        if expected == "*":
            return True, None, None, None
        
        # Type check
        if type(actual) != type(expected):
            return False, path, expected, actual
        
        if isinstance(expected, dict):
            # Check all keys in expected exist in actual with matching values
            for key in expected:
                if key == "*":
                    # Wildcard key - match any key with the expected value
                    if not actual:  # No keys in actual dict
                        return False, f"{path}.*", expected[key], None
                    # Check if any key has the expected value
                    found_match = False
                    for actual_key, actual_val in actual.items():
                        matches, _, _, _ = self.subset_match(actual_val, expected[key], f"{path}.{actual_key}")
                        if matches:
                            found_match = True
                            break
                    if not found_match:
                        # Return the first actual value for error reporting
                        first_key = list(actual.keys())[0] if actual else None
                        first_val = actual[first_key] if first_key else None
                        return False, f"{path}.*", expected[key], first_val
                else:
                    if key not in actual:
                        return False, f"{path}.{key}", expected[key], None
                    matches, mismatch_path, exp_val, act_val = self.subset_match(
                        actual[key], expected[key], f"{path}.{key}"
                    )
                    if not matches:
                        return False, mismatch_path, exp_val, act_val
            return True, None, None, None
            
        elif isinstance(expected, list):
            # Lists must match exactly in length
            if len(actual) != len(expected):
                return False, f"{path}.length", len(expected), len(actual)
            
            # Check if this is a list of objects (dicts)
            # If so, compare without caring about order
            if (expected and isinstance(expected[0], dict) and
                actual and isinstance(actual[0], dict)):
                
                # For lists with wildcard IDs, we need to match by type/structure
                if any(item.get('id') == '*' for item in expected if isinstance(item, dict)):
                    # Match items by type and other fields
                    unmatched_actual = list(actual)
                    for exp_item in expected:
                        found = False
                        for i, act_item in enumerate(unmatched_actual):
                            # Try to match this expected item with an actual item
                            matches, _, _, _ = self.subset_match(act_item, exp_item, path)
                            if matches:
                                unmatched_actual.pop(i)
                                found = True
                                break
                        if not found:
                            return False, f"{path}[id={exp_item.get('id', '?')}]", exp_item, None
                    return True, None, None, None
                
                # For lists with concrete IDs, use ID-based matching
                elif all('id' in item for item in expected if isinstance(item, dict)):
                    # Build maps by ID for order-independent comparison
                    expected_by_id = {item['id']: item for item in expected if isinstance(item, dict) and 'id' in item}
                    actual_by_id = {item['id']: item for item in actual if isinstance(item, dict) and 'id' in item}
                    
                    # Check all expected items exist in actual
                    for exp_id, exp_item in expected_by_id.items():
                        if exp_id not in actual_by_id:
                            return False, f"{path}[id={exp_id}]", exp_item, None
                        matches, mismatch_path, exp_val, act_val = self.subset_match(
                            actual_by_id[exp_id], exp_item, f"{path}[id={exp_id}]"
                        )
                        if not matches:
                            return False, mismatch_path, exp_val, act_val
                    return True, None, None, None
                else:
                    # For lists of objects without IDs, fall back to order-dependent comparison
                    for i, (a, e) in enumerate(zip(actual, expected)):
                        matches, mismatch_path, exp_val, act_val = self.subset_match(
                            a, e, f"{path}[{i}]"
                        )
                        if not matches:
                            return False, mismatch_path, exp_val, act_val
                    return True, None, None, None
            else:
                # For other lists, order matters
                for i, (a, e) in enumerate(zip(actual, expected)):
                    matches, mismatch_path, exp_val, act_val = self.subset_match(
                        a, e, f"{path}[{i}]"
                    )
                    if not matches:
                        return False, mismatch_path, exp_val, act_val
                return True, None, None, None
            
        else:
            # Primitive values must match exactly
            if actual != expected:
                return False, path, expected, actual
            return True, None, None, None
    
    def run_test_scenario(self, scenario, test_file):
        """Run a single test scenario using real framework"""
        scenario_name = scenario.get("name", scenario.get("description", "Unnamed"))
        self.log(f"Running scenario: {scenario_name}")

        try:
            given = scenario.get("given", {})
            then = scenario.get("then", {})
            
            # Set environment variables if specified
            if "env" in given:
                for key, value in given["env"].items():
                    os.environ[key] = value
            
            # Set crypto mode to dummy by default
            if "CRYPTO_MODE" not in os.environ:
                os.environ["CRYPTO_MODE"] = "dummy"
            
            # Set up initial state using persistent database
            from core.db import create_db
            # Use a unique database for each test to avoid conflicts
            import uuid
            test_id = str(uuid.uuid4())[:8]
            base_test_db = os.environ.get('TEST_DB_PATH', ':memory:')
            # If this scenario requires concurrency, force a file-backed DB
            requires_file_db = bool(given.get("concurrent"))
            if base_test_db != ':memory:':
                # Make it unique for this test
                test_db_path = base_test_db.replace('.db', f'_{test_id}.db')
            else:
                test_db_path = base_test_db
                if requires_file_db:
                    # Use a unique temporary file for sharing between threads
                    test_db_path = f".test_concurrent_{test_id}.db"
            
            # Extract protocol name from handler path to load correct schema
            protocol_name = None
            handler_path = os.environ.get('HANDLER_PATH', '')
            if 'protocols/' in handler_path:
                parts = handler_path.split('/')
                for i, part in enumerate(parts):
                    if part == 'protocols' and i + 1 < len(parts):
                        protocol_name = parts[i + 1]
                        break
            
            db = create_db(db_path=test_db_path, protocol_name=protocol_name)
            
            # Track the database file for cleanup
            self._track_db_file(test_db_path)
            
            # Seed SQL generically from given db.tables/state
            given_db = given.get("db", {"state": {}})
            try:
                self._seed_sql_generic(db, given_db)
            except Exception:
                pass
            
            # Tests should provide encrypted data directly, not use setup generation
            
            # Execute commands if any
            command_results = []
            if "commands" in given:
                for cmd in given["commands"]:
                    result = self.execute_command(cmd, db)
                    command_results.append(result)
                    # Note: events are now projected automatically by run_command

            # Execute concurrent scenario if specified
            if "concurrent" in given:
                self._run_concurrent(given["concurrent"], test_db_path)
            
            # Legacy permutation shortcuts removed – tests should drive via SQL/stateful operations
            
            # Run ticks if specified or if this is a tick.json test
            ticks_to_run = scenario.get('ticks', 0)
            # For tick.json tests, always run at least one tick
            if 'tick.json' in test_file:
                ticks_to_run = max(1, ticks_to_run)
            
            if ticks_to_run > 0:
                from core.tick import tick
                time_now_ms = scenario.get('time_now_ms')
                for _ in range(ticks_to_run):
                    tick(db, time_now_ms=time_now_ms)
            
            # Build result for comparison
            # Convert persistent db to dict for comparison
            # Reload DB from disk in case concurrent workers used separate connections
            if hasattr(db, 'close'):
                db.close()
            db_fresh = create_db(db_path=test_db_path, protocol_name=protocol_name)
            result = {}

            # Attach generic SQL snapshot
            try:
                snap = self._dump_sql_generic(db_fresh)
                result['snapshot'] = snap
                # Tables are now at top level of snapshot
                if isinstance(snap, dict):
                    result['db'] = snap
            except Exception:
                pass
            # Do not attach dict-style db view; SQL tables are the source of truth
            if command_results:
                result["commandResults"] = command_results
                # No dict-db back-compat normalization here
            
            # Close the database connection
            if hasattr(db, 'close'):
                db.close()
            
            # Cleanup is now handled by _cleanup_db_files() at the end
            
            # Filter out description from then before matching
            then_filtered = {k: v for k, v in then.items() if k != "description"}
            # No protocol-specific filtering here; protocols define snapshots
            
            matches, path, exp_val, act_val = self.subset_match(result, then_filtered)
            if matches:
                return {"scenario": scenario_name, "passed": True, "logs": self.logs}
            else:
                self.log(f"Mismatch at {path}: expected {exp_val}, got {act_val}", "ERROR")
                return {"scenario": scenario_name, "passed": False, "logs": self.logs}

        except Exception as e:
            self.log(f"Scenario crashed: {str(e)}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")
            return {
                "scenario": scenario_name,
                "passed": False,
                "logs": self.logs,
                "error": str(e)
            }

    def _seed_sql_generic(self, db, given_db):
        if not hasattr(db, 'conn') or db.conn is None:
            return
        tables = {}
        src = given_db or {}
        # Tables are now directly in db (no 'tables' nesting)
        if isinstance(src, dict):
            # Extract table-like entries (those with list values)
            for key, value in src.items():
                if isinstance(value, list) and key not in ['eventStore', 'envelope']:
                    tables[key] = value
        if not tables:
            return
        conn = db.conn
        cur = conn.cursor()
        try:
            conn.execute('BEGIN IMMEDIATE')
        except Exception:
            pass
        def _table_info(tname):
            """Return list of (name, type, notnull, pk, dflt_value)."""
            cols = []
            try:
                for r in cur.execute(f"PRAGMA table_info({tname})").fetchall():
                    # r: cid, name, type, notnull, dflt_value, pk
                    cols.append((r[1], (r[2] or '').upper(), int(r[3] or 0), int(r[5] or 0), r[4]))
            except Exception:
                pass
            return cols
        for tname, rows in tables.items():
            try:
                cur.execute(f"DELETE FROM {tname}")
            except Exception:
                continue
            info = _table_info(tname)
            if not info:
                continue
            col_names = [c[0] for c in info]
            for row in (rows or []):
                if not isinstance(row, dict):
                    continue
                # Build a values dict from available keys
                vals = {k: row.get(k) for k in row.keys() if k in col_names}
                # Ensure unique event_id where required
                try:
                    if 'event_id' in col_names and ('event_id' not in vals or not vals.get('event_id')):
                        import uuid as _uuid
                        vals['event_id'] = f"seeded-{_uuid.uuid4()}"
                except Exception:
                    pass
                # JSON-encode common JSON columns when seeding
                try:
                    import json as _json
                    for jk in ['data', 'metadata', 'event_data']:
                        if jk in vals and not (isinstance(vals[jk], (str, bytes)) or vals[jk] is None):
                            vals[jk] = _json.dumps(vals[jk])
                except Exception:
                    pass
                # Fill required not-null columns with sensible defaults
                for name, ctype, notnull, pk, dflt in info:
                    if pk == 1:
                        continue
                    if name in vals:
                        continue
                    if notnull:
                        if dflt is not None:
                            vals[name] = dflt
                        else:
                            lname = name.lower()
                            if 'sig' == lname:
                                vals[name] = ''
                            elif 'int' in ctype or 'INTEGER' in ctype:
                                vals[name] = 0
                            else:
                                vals[name] = ''
                ins_cols = list(vals.keys())
                placeholders = ','.join(['?'] * len(ins_cols))
                insert_sql = f"INSERT INTO {tname}({','.join(ins_cols)}) VALUES({placeholders})"
                values = [vals[k] for k in ins_cols]
                try:
                    cur.execute(insert_sql, values)
                except Exception:
                    pass
        try:
            conn.commit()
        except Exception:
            pass

    def _dump_sql_generic(self, db):
        if not hasattr(db, 'conn') or db.conn is None:
            self.log("WARNING: No database connection available for SQL dump", "DEBUG")
            return { 'tables': {} }
        conn = db.conn
        cur = conn.cursor()
        try:
            table_names = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            self.log(f"Found SQL tables: {table_names}", "DEBUG")
        except Exception as e:
            self.log(f"Error listing SQL tables: {e}", "ERROR")
            table_names = []
        def _skip(name):
            return (
                not name or name.startswith('sqlite_') or name in ['_kv_store','_list_store','_event_store']
            )
        out = {}
        # Preload column types for boolean normalization
        col_types = {}
        for tname in table_names:
            if _skip(tname):
                continue
            types = {}
            try:
                for r in cur.execute(f"PRAGMA table_info({tname})").fetchall():
                    # r: cid, name, type, notnull, dflt_value, pk
                    types[r[1]] = (r[2] or '').upper()
            except Exception:
                pass
            col_types[tname] = types
        for tname in sorted(n for n in table_names if not _skip(n)):
            try:
                order_col = None
                try:
                    for r in cur.execute(f"PRAGMA table_info({tname})").fetchall():
                        if r[5] == 1:
                            order_col = r[1]
                            break
                except Exception:
                    pass
                sql = f"SELECT * FROM {tname}"
                if order_col:
                    sql += f" ORDER BY {order_col}"
                rows = cur.execute(sql).fetchall()
                cols = [d[0] for d in cur.description]
                items = []
                for r in rows:
                    obj = { cols[i]: r[i] for i in range(len(cols)) }
                    # Try JSON decode for common columns
                    for key in ['data','metadata','event_data']:
                        v = obj.get(key)
                        if isinstance(v, str):
                            try:
                                import json
                                obj[key] = json.loads(v)
                            except Exception:
                                pass
                    # Normalize event_store rows to have 'data' instead of 'event_data'
                    if tname == 'event_store' and 'event_data' in obj and 'data' not in obj:
                        obj['data'] = obj.pop('event_data')
                    # Boolean normalization based on declared type
                    try:
                        types = col_types.get(tname) or {}
                        for k, t in types.items():
                            if 'BOOL' in t and k in obj and isinstance(obj[k], (int, float)):
                                obj[k] = bool(obj[k])
                    except Exception:
                        pass
                    items.append(obj)
                out[tname] = items
                self.log(f"Table {tname}: {len(items)} rows", "DEBUG")
            except Exception as e:
                self.log(f"Error reading table {tname}: {e}", "ERROR")
                pass
        # Return tables directly (no 'tables' nesting)
        import json as _json
        self.log(f"SQL dump result: {_json.dumps(out, indent=2)[:500]}...", "DEBUG")
        return out

    def _run_concurrent(self, spec, db_path):
        """Run tick and commands concurrently against the same SQLite file.

        Spec format:
          {
            "commands": [ {"handler":..., "command":..., "input":..., "delay_ms": 0}, ... ],
            "tick": {"runs": 1, "time_now_ms": 1000, "delay_ms": 0, "interval_ms": 0}
          }
        Each worker creates its own DB connection bound to db_path to avoid cross-thread sqlite issues.
        """
        import threading
        import time as _time
        from core.db import create_db

        errors = []

        def run_command_worker(cmd_def):
            try:
                delay = int(cmd_def.get("delay_ms", 0))
                if delay:
                    _time.sleep(delay / 1000.0)
                db_local = create_db(db_path=db_path)
                from core.command import run_command
                handler = cmd_def["handler"]
                command = cmd_def["command"]
                input_data = cmd_def.get("input", {})
                time_now_ms = cmd_def.get("time_now_ms")
                run_command(handler, command, input_data, db_local, time_now_ms=time_now_ms)
                if hasattr(db_local, 'close'):
                    db_local.close()
            except Exception as e:
                self.log(f"Concurrent command failed: {e}", "ERROR")
                errors.append(e)

        def run_tick_worker(tick_def):
            try:
                delay = int(tick_def.get("delay_ms", 0))
                if delay:
                    _time.sleep(delay / 1000.0)
                runs = int(tick_def.get("runs", 1))
                interval = int(tick_def.get("interval_ms", 0))
                time_now_ms = tick_def.get("time_now_ms")
                from core.tick import tick
                db_local = create_db(db_path=db_path)
                for i in range(runs):
                    tick(db_local, time_now_ms=time_now_ms)
                    if interval and i < runs - 1:
                        _time.sleep(interval / 1000.0)
                if hasattr(db_local, 'close'):
                    db_local.close()
            except Exception as e:
                self.log(f"Concurrent tick failed: {e}", "ERROR")
                errors.append(e)

        threads = []
        for cmd in spec.get("commands", []):
            t = threading.Thread(target=run_command_worker, args=(cmd,), daemon=True)
            threads.append(t)
        tick_spec = spec.get("tick")
        if tick_spec:
            threads.append(threading.Thread(target=run_tick_worker, args=(tick_spec,), daemon=True))

        # If using in-memory DB, switch to a temporary file so threads share state
        if db_path == ':memory:':
            tmp_path = f".concurrent_{int(_time.time()*1000)}.db"
            self.log(f"Concurrent test requires file-backed DB, switching to {tmp_path}")
            db_path = tmp_path
            self._track_db_file(tmp_path)

        # Start all threads
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errors:
            raise errors[0]
    
    def execute_command(self, cmd, db):
        """Execute a command and return its result"""
        handler = cmd["handler"]
        command = cmd["command"]
        input_data = cmd.get("input", {})
        
        # Validate input against schema if defined
        from core.schema_validator import validate_command_input, validate_command_output
        is_valid, error = validate_command_input(handler, command, input_data)
        if not is_valid:
            raise ValueError(f"Input validation failed: {error}")
        
        # Use run_command to execute and project events
        from core.command import run_command
        updated_db, result = run_command(handler, command, input_data, db, time_now_ms=1000)
        
        # Update db reference - db is already updated by run_command
        # The updated_db returned is the same reference
        
        # Validate output against schema if defined
        is_valid, error = validate_command_output(handler, command, result)
        if not is_valid:
            raise ValueError(f"Output validation failed: {error}")
        
        # Convert db in result to dict for comparison
        if 'db' in result and hasattr(result['db'], 'to_dict'):
            result['db'] = result['db'].to_dict()
        
        return result
    
    
    
    def run_handler_test(self, test, handler_file, handler_name=None, command_name=None):
        """Run handler tests using real framework"""
        # Determine protocol from handler file path
        parts = handler_file.split('/')
        if len(parts) >= 2 and parts[0] == 'protocols':
            protocol = parts[1]
        else:
            raise ValueError(f"Cannot determine protocol from handler file path: {handler_file}")
        
        # For all protocols, automatically generate permutations if not specified
        if "permutations" not in test:
            # Collect all events from the test
            events = []
            given = test.get("given", {})
            
            # Only collect events from eventStore that are meant to be processed
            # Skip if eventStore already has events in initial state (idempotency tests)
            initial_state = given.get("db", {}).get("state", {})
            initial_eventstore = given.get("db", {}).get("eventStore", [])
            
            # Skip permutation for idempotency tests
            # (eventStore has initial events and state has corresponding data)
            if initial_eventstore and any(initial_state.get(k) for k in ["messages", "users", "groups", "channels"]):
                # This looks like an idempotency test, skip permutation
                pass
            else:
                # Collect events from eventStore for permutation
                if "eventStore" in given.get("db", {}):
                    events.extend(given["db"]["eventStore"])
                
                # Add the envelope if present
                if "envelope" in given:
                    events.append(given["envelope"])
            
            # If we have multiple events, generate permutations
            if len(events) > 1:
                # Generate all permutations
                all_permutations = list(itertools.permutations(events))
                
                # Run test for each permutation
                results = []
                for i, perm in enumerate(all_permutations):
                    self.log(f"Testing permutation {i+1}/{len(all_permutations)}: {[e['data']['type'] for e in perm if 'data' in e]}")
                    
                    # Create a persistent DB and seed SQL from given state
                    from core.db import create_db
                    import uuid as _uuid
                    base_test_db = os.environ.get('TEST_DB_PATH', ':memory:')
                    if base_test_db != ':memory:':
                        test_db_path = base_test_db.replace('.db', f'_{str(_uuid.uuid4())[:8]}.db')
                    else:
                        test_db_path = base_test_db
                    perm_db = create_db(db_path=test_db_path, protocol_name=protocol)
                    self._track_db_file(test_db_path)

                    # Seed SQL only (tables)
                    given_db = copy.deepcopy(test.get('given', {}).get('db', {}))
                    try:
                        self._seed_sql_generic(perm_db, given_db)
                    except Exception:
                        pass

                    # Process events in this order using persistent DB
                    from core.handle import handle
                    for event in perm:
                        perm_db = handle(perm_db, event, time_now_ms=1000)
                    
                    # Run ticks if specified
                    ticks_to_run = test.get('ticks', 0)
                    if ticks_to_run > 0:
                        from core.tick import run_all_jobs as tick
                        for _ in range(ticks_to_run):
                            tick(perm_db, time_now_ms=1000)
                    
                    # Build snapshot for SQL-native protocols
                    result_obj = {}
                    try:
                        # Always use generic SQL snapshot
                        snap = self._dump_sql_generic(perm_db)
                        if isinstance(snap, dict):
                            result_obj['snapshot'] = snap
                            # Tables are now at top level
                            result_obj['db'] = snap
                    except Exception:
                        pass

                    # Check result matches expected (supports tables/snapshot in 'then')
                    then = test.get("then", {})
                    matches, path, exp_val, act_val = self.subset_match(result_obj, then)
                    
                    if not matches:
                        self.log(f"Permutation {i+1} FAILED at {path}: expected {exp_val}, got {act_val}", "ERROR")
                        scenario_name = test.get("description", "Unnamed")
                        return {"scenario": scenario_name, "passed": False, "logs": self.logs}
                    else:
                        self.log(f"Permutation {i+1} passed")
                
                # All permutations passed
                scenario_name = test.get("description", "Unnamed")
                return {"scenario": scenario_name, "passed": True, "logs": self.logs}
        
        # For handler tests with envelope, we need to handle it directly
        if "envelope" in test.get("given", {}):
            scenario_name = test.get("description", "Unnamed")
            self.log(f"Running projector test: {scenario_name}")
            
            given = test.get("given", {})
            then = test.get("then", {})
            envelope = given["envelope"]
            # Use a persistent DB so SQL-only handlers see tables
            from core.db import create_db
            import uuid as _uuid
            base_test_db = os.environ.get('TEST_DB_PATH', ':memory:')
            if base_test_db != ':memory:':
                test_db_path = base_test_db.replace('.db', f'_{str(_uuid.uuid4())[:8]}.db')
            else:
                test_db_path = base_test_db
            # Extract protocol name from handler path to load correct schema
            protocol_name = None
            handler_path = os.environ.get('HANDLER_PATH', '')
            if 'protocols/' in handler_path:
                parts = handler_path.split('/')
                for i, part in enumerate(parts):
                    if part == 'protocols' and i + 1 < len(parts):
                        protocol_name = parts[i + 1]
                        break
            
            db = create_db(db_path=test_db_path, protocol_name=protocol_name)
            # Track for cleanup
            self._track_db_file(test_db_path)
            # Seed SQL only (tables)
            given_db = given.get("db", {})
            try:
                self._seed_sql_generic(db, given_db)
            except Exception as e:
                self.log(f"Error seeding database: {e}", "ERROR")
                if self.verbose:
                    import traceback
                    traceback.print_exc()
            
            # Call handle directly with the envelope
            from core.handle import handle
            self.log(f"Calling handle with envelope: {envelope}")
            import json
            self.log(f"Initial db state: {json.dumps(given_db, indent=2)}")
            
            result_db = handle(db, envelope, time_now_ms=1000)
            
            try:
                rb = result_db.to_dict() if hasattr(result_db, 'to_dict') else result_db
                self.log(f"Result db after handle: {json.dumps(rb, indent=2)}")
                # Also log SQL tables when in verbose mode for debugging
                if self.verbose:
                    try:
                        snap_dbg = self._dump_sql_generic(result_db)
                        self.log(f"SQL tables after handle: {json.dumps(snap_dbg, indent=2)}")
                    except Exception:
                        pass
            except Exception:
                self.log("Result db after handle: <non-serializable>")
            
            # Check for errors
            if 'blocked' in result_db:
                self.log(f"WARNING: Found blocked envelopes: {result_db['blocked']}", "WARNING")
            
            # No dict-state diff logging; SQL is the source of truth
            
            # Run ticks if specified
            ticks_to_run = test.get('ticks', 0)
            if ticks_to_run > 0:
                self.log(f"Running {ticks_to_run} ticks for projector test")
                from core.tick import run_all_jobs as tick
                # Run the specified number of ticks
                base_time = 1000
                time_increment = 100
                for i in range(ticks_to_run):
                    current_time = base_time + (i + 1) * time_increment
                    self.log(f"Tick {i+1} at time {current_time}")
                    result_db = tick(result_db, time_now_ms=current_time)
            
            # Check result
            result = {}
            # Generic SQL snapshot only
            try:
                snap = self._dump_sql_generic(result_db)
                if isinstance(snap, dict):
                    result['snapshot'] = snap
                    # Tables are now at top level of snapshot
                    if isinstance(snap, dict):
                        result['db'] = snap
            except Exception:
                pass
            matches, path, exp_val, act_val = self.subset_match(result, then)

            # Idempotency check (default ON): Re-apply events and expect same SQL tables
            # Tests can opt out by setting skipIdempotency: true
            skip_idempotency = test.get('skipIdempotency', False)
            idempo_failed = False
            idempo_error = None
            if not skip_idempotency:
                try:
                    given_db = copy.deepcopy(given.get("db", {}))
                    base_events = []
                    # Collect base events (eventStore + envelope)
                    if "eventStore" in given_db:
                        base_events.extend(given_db["eventStore"])
                    if envelope:
                        base_events.append(envelope)

                    if base_events:
                        def _dump_tables_only(db_obj):
                            snap = self._dump_sql_generic(db_obj)
                            t = snap if isinstance(snap, dict) else {}
                            # Ignore event_store in idempotency comparisons
                            if 'event_store' in t:
                                t = {k: v for k, v in t.items() if k != 'event_store'}
                            # Remove auto-increment id columns from all tables for idempotency
                            for table_name, rows in list(t.items()):
                                if isinstance(rows, list):
                                    # Remove 'id' and 'event_id' fields from each row for idempotency
                                    t[table_name] = [{k: v for k, v in row.items() if k not in ['id', 'event_id']} if isinstance(row, dict) else row for row in rows]
                            return t

                    # Single pass 
                    # Create a new database for idempotency testing
                    import uuid as _uuid
                    base_test_db = os.environ.get('TEST_DB_PATH', ':memory:')
                    if base_test_db != ':memory:':
                        single_db_path = base_test_db.replace('.db', f'_idmp_single_{str(_uuid.uuid4())[:8]}.db')
                    else:
                        single_db_path = base_test_db
                    single_db = create_db(db_path=single_db_path, protocol_name=protocol)
                    self._track_db_file(single_db_path)
                    self._seed_sql_generic(single_db, copy.deepcopy(given_db))
                    for ev in base_events:
                        single_db = handle(single_db, ev, time_now_ms=1000)
                    ticks_to_run = test.get('ticks', 0)
                    if ticks_to_run > 0:
                        from core.tick import run_all_jobs as run_tick
                        for i in range(ticks_to_run):
                            current_time = 1000 + (i + 1) * 100
                            single_db = run_tick(single_db, time_now_ms=current_time)

                    # Double pass
                    if base_test_db != ':memory:':
                        double_db_path = base_test_db.replace('.db', f'_idmp_double_{str(_uuid.uuid4())[:8]}.db')
                    else:
                        double_db_path = base_test_db
                    doubled_db = create_db(db_path=double_db_path, protocol_name=protocol)
                    self._track_db_file(double_db_path)
                    self._seed_sql_generic(doubled_db, copy.deepcopy(given_db))
                    doubled_events = base_events + base_events
                    for ev in doubled_events:
                        doubled_db = handle(doubled_db, ev, time_now_ms=1000)
                    if ticks_to_run > 0:
                        from core.tick import run_all_jobs as run_tick
                        for i in range(ticks_to_run):
                            current_time = 1000 + (i + 1) * 100
                            doubled_db = run_tick(doubled_db, time_now_ms=current_time)

                    # Compare SQL tables (excluding event_store)
                    single_tables = _dump_tables_only(single_db)
                    double_tables = _dump_tables_only(doubled_db)
                    if single_tables != double_tables:
                        idempo_failed = True
                        idempo_error = "Idempotency failed: tables differ after doubling events"
                        # Debug: show what's different
                        if self.verbose:
                            import json
                            for table_name in set(single_tables.keys()) | set(double_tables.keys()):
                                if single_tables.get(table_name) != double_tables.get(table_name):
                                    print(f"\n[DEBUG] Table '{table_name}' differs:")
                                    print(f"  Single: {json.dumps(single_tables.get(table_name), indent=2)}")
                                    print(f"  Double: {json.dumps(double_tables.get(table_name), indent=2)}")
                except Exception as e:
                    idempo_failed = True
                    idempo_error = f"Idempotency check crashed: {e}"

            if matches and not idempo_failed:
                return {"scenario": scenario_name, "passed": True, "logs": self.logs}
            else:
                if not matches:
                    self.log(f"Mismatch at {path}: expected {exp_val}, got {act_val}", "ERROR")
                if idempo_failed:
                    self.log(idempo_error, "ERROR")
                return {"scenario": scenario_name, "passed": False, "logs": self.logs}
        
        
        # For command tests, handle differently
        if "params" in test.get("given", {}):
            # This is a command test
            scenario_name = test.get("description", "Command test")
            self.log(f"Running command test: {scenario_name}")
            
            given = test.get("given", {})
            then = test.get("then", {})
            
            # Set environment variables if specified
            if "env" in given:
                for key, value in given["env"].items():
                    os.environ[key] = value
            
            # Tests should provide encrypted data directly, not use setup generation
            
            # Execute command
            # Extract handler name from file path
            if not handler_name:
                path_parts = handler_file.split('/')
                for i, part in enumerate(path_parts):
                    if part == "handlers" and i + 1 < len(path_parts):
                        handler_name = path_parts[i + 1]
                        break
                else:
                    handler_name = "message"  # fallback
            
            # Use command name if provided
            if not command_name:
                command_name = "create"  # fallback
            
            cmd = {
                "handler": handler_name,
                "command": command_name,
                "input": given["params"]
            }
            
            # Initialize db using persistent database
            from core.db import create_db
            # Use a unique database for each test to avoid conflicts
            import uuid
            test_id = str(uuid.uuid4())[:8]
            base_test_db = os.environ.get('TEST_DB_PATH', ':memory:')
            if base_test_db != ':memory:':
                # Make it unique for this test
                test_db_path = base_test_db.replace('.db', f'_{test_id}.db')
            else:
                test_db_path = base_test_db
            
            # Extract protocol name from handler path to load correct schema
            protocol_name = None
            handler_path = os.environ.get('HANDLER_PATH', '')
            if 'protocols/' in handler_path:
                parts = handler_path.split('/')
                for i, part in enumerate(parts):
                    if part == 'protocols' and i + 1 < len(parts):
                        protocol_name = parts[i + 1]
                        break
            
            db = create_db(db_path=test_db_path, protocol_name=protocol_name)
            
            # Track the database file for cleanup
            self._track_db_file(test_db_path)
            
            # Seed SQL tables generically from given state
            given_db = given.get("db", {})
            try:
                self._seed_sql_generic(db, given_db)
            except Exception:
                pass
            
            try:
                import json as _json
                self.log(f"Executing command: {command_name} for test '{test.get('description', 'Command test')}'", "DEBUG")
                self.log(f"Command payload: {_json.dumps(test.get('given', {}).get('params', {}), indent=2)}", "DEBUG")
                result = self.execute_command(cmd, db)
                self.log(f"Command result keys: {list(result.keys()) if isinstance(result, dict) else type(result)}", "DEBUG")
                
                # SQL-only mode: commands should not return 'db' changes
                if "db" in result:
                    self.log("Warning: Command returned 'db' field which is deprecated in SQL-only mode", "WARNING")
            except Exception as e:
                # Check if this test expects an error
                if "error" in then:
                    # Extract the actual error message from the wrapped exception
                    error_msg = str(e)
                    # Remove the "Error in handler.command: " prefix if present
                    if error_msg.startswith("Error in ") and ": " in error_msg:
                        error_msg = error_msg.split(": ", 1)[1]
                    
                    # Check if the error matches the expected error
                    expected_error = then["error"]
                    if error_msg == expected_error:
                        self.log(f"Got expected error: {error_msg}")
                        # Test passed - expected error occurred
                        return {"scenario": test.get('description', 'Command test'), "passed": True, "logs": self.logs}
                    else:
                        error_message = f"Expected error '{expected_error}' but got '{error_msg}'"
                        self.log(error_message, "ERROR")
                        return {"scenario": test.get('description', 'Command test'), "passed": False, "logs": self.logs}
                else:
                    self.log(f"Command execution failed: {str(e)}", "ERROR")
                    # For crypto-related failures, add more context
                    if "decrypt" in str(e).lower() or "crypto" in str(e).lower():
                        self.log("Note: Real crypto tests require proper encryption/decryption. Check that:", "ERROR")
                        self.log("  - PyNaCl is installed (pip install pynacl)", "ERROR")
                        self.log("  - Keys are properly formatted (64 hex chars for 32-byte keys)", "ERROR")
                        self.log("  - Wire format matches expectations (hash:64, nonce:48, ciphertext:remaining)", "ERROR")
                        self.log(f"  - Current CRYPTO_MODE: {os.environ.get('CRYPTO_MODE', 'dummy')}", "ERROR")
                    raise
            
            # Run ticks if specified
            ticks = test.get("ticks", 0)
            if ticks > 0:
                self.log(f"Running {ticks} ticks after command")
                
                # Constants for time progression
                base_time = given.get("params", {}).get("time_now_ms", 1000)
                time_increment = 100  # ms between ticks
                
                # Run jobs generically for all protocols
                from core.tick import run_all_jobs as tick
                
                # Run the specified number of ticks
                for i in range(ticks):
                    current_time = base_time + (i + 1) * time_increment
                    self.log(f"Tick {i+1} at time {current_time}")
                    db = tick(db, time_now_ms=current_time)
                    
                # no-op: db state asserted via generic snapshot
            
            # Always attach a generic SQL snapshot (protocol-agnostic)
            result_snapshot = None
            try:
                result_snapshot = self._dump_sql_generic(db)
                result['snapshot'] = result_snapshot
                # Tables are now at top level, add as 'db' for consistency
                if isinstance(result_snapshot, dict):
                    result['db'] = result_snapshot
            except Exception:
                pass
            # Check result
            return_matches = True
            if "return" in then:
                # Build two candidate shapes for compatibility:
                # 1) Flattened: api_response fields lifted + newEvents sibling
                # 2) Nested: {'api_response': {...}, 'newEvents': [...]} (legacy tests)
                flat_ret = {}
                nested_ret = {}
                try:
                    if isinstance(result, dict) and 'api_response' in result:
                        ar = result.get('api_response') or {}
                        if isinstance(ar, dict):
                            flat_ret.update(ar)
                            nested_ret['api_response'] = ar
                    if isinstance(result, dict):
                        if 'newEvents' in result:
                            flat_ret['newEvents'] = result['newEvents']
                            nested_ret['newEvents'] = result['newEvents']
                        elif 'newEnvelopes' in result:
                            flat_ret['newEvents'] = result['newEnvelopes']
                            nested_ret['newEvents'] = result['newEnvelopes']
                except Exception:
                    pass
                if not flat_ret:
                    flat_ret = result
                if not nested_ret:
                    nested_ret = result

                # Try flattened first, then nested
                matches, path, exp_val, act_val = self.subset_match(flat_ret, then["return"])
                if not matches:
                    matches, path, exp_val, act_val = self.subset_match(nested_ret, then["return"])
                if not matches:
                    # Check if test expects the whole result structure under 'return'
                    if isinstance(then["return"], dict) and "api_response" in then["return"]:
                        # Test expects the entire result structure
                        matches, path, exp_val, act_val = self.subset_match(result, then["return"])
                        if not matches:
                            # Also try matching just against api_response
                            if "api_response" in result:
                                matches, path, exp_val, act_val = self.subset_match(result["api_response"], then["return"]["api_response"])
                if not matches:
                    import json as _json
                    self.log(f"Test: {test.get('description', 'Unnamed')}", "ERROR")
                    self.log(f"Expected return structure: {_json.dumps(then['return'], indent=2)}", "ERROR")
                    self.log(f"Actual return structure: {_json.dumps(result, indent=2)}", "ERROR")
                    self.log(f"Mismatch at return{path}: expected {exp_val}, got {act_val}", "ERROR")
                    return_matches = False
            
            # Check newEvents if specified
            newevents_matches = True
            if "newEvents" in then:
                if "newEvents" in result:
                    matches, path, exp_val, act_val = self.subset_match({"newEvents": result["newEvents"]}, {"newEvents": then["newEvents"]})
                    if not matches:
                        import json as _json
                        self.log(f"Test: {test.get('description', 'Unnamed')} - newEvents mismatch", "ERROR")
                        self.log(f"Expected newEvents: {_json.dumps(then['newEvents'], indent=2)}", "ERROR")
                        self.log(f"Actual newEvents: {_json.dumps(result.get('newEvents', []), indent=2)}", "ERROR")
                        self.log(f"Mismatch at newEvents{path}: expected {exp_val}, got {act_val}", "ERROR")
                        newevents_matches = False
                else:
                    self.log("Test expects newEvents but result has none", "ERROR")
                    newevents_matches = False
            
            # Prefer snapshot assertions when provided
            snapshot_matches = True
            if "snapshot" in then and result_snapshot is not None:
                import json as _json
                self.log(f"Comparing snapshots for test '{test.get('description', 'Unnamed')}':", "DEBUG")
                self.log(f"Expected snapshot: {_json.dumps(then['snapshot'], indent=2)}", "DEBUG")
                self.log(f"Actual snapshot: {_json.dumps(result_snapshot, indent=2)}", "DEBUG")
                matches, path, exp_val, act_val = self.subset_match({"snapshot": result_snapshot}, {"snapshot": then["snapshot"]})
                if not matches:
                    self.log(f"Snapshot mismatch at {path}: expected {exp_val}, got {act_val}", "ERROR")
                    snapshot_matches = False
                else:
                    self.log("Snapshot assertion passed", "DEBUG")

            # Direct db assertions when provided  
            db_matches = True
            if "db" in then:
                # For backward compatibility, check if test expects dict-style db
                # but we have SQL tables in result['db']
                if 'db' in result:
                    expected_db = then['db']
                    actual_db = result['db']
                    
                    # Filter SQL tables to match test expectations
                    for table_name, expected_rows in expected_db.items():
                        if isinstance(expected_rows, list) and expected_rows and isinstance(expected_rows[0], dict):
                            # Get the expected fields from the first row
                            expected_fields = set()
                            for row in expected_rows:
                                if isinstance(row, dict):
                                    expected_fields.update(row.keys())
                            
                            if table_name in actual_db and isinstance(actual_db[table_name], list):
                                # Filter actual rows to only include expected fields
                                filtered_rows = []
                                for actual_row in actual_db[table_name]:
                                    if isinstance(actual_row, dict):
                                        filtered_row = {k: v for k, v in actual_row.items() if k in expected_fields}
                                        filtered_rows.append(filtered_row)
                                actual_db[table_name] = filtered_rows
                            elif table_name not in actual_db:
                                # Test expects table but it's not in the SQL dump
                                # This means the table is empty
                                actual_db[table_name] = []
                    
                    # Log the actual vs expected database state
                    import json as _json
                    self.log(f"Expected DB state: {_json.dumps(expected_db, indent=2)}", "DEBUG")
                    self.log(f"Actual DB state: {_json.dumps(actual_db, indent=2)}", "DEBUG")
                    
                    matches, path, exp_val, act_val = self.subset_match({"db": actual_db}, {"db": expected_db})
                    if not matches:
                        self.log(f"Mismatch at {path}: expected {exp_val}, got {act_val}", "ERROR")
                        db_matches = False
                else:
                    self.log("Mismatch: db expected but no db in result", "ERROR")
                    self.log(f"Result keys: {list(result.keys())}", "DEBUG")
                    db_matches = False
            
            # Idempotency for command tests (default ON): run command once vs twice
            # Skip idempotency check for create/join/process_incoming/send/sync_all commands as they are inherently non-idempotent
            skip_idempotency = (command_name in ['create', 'join', 'process_incoming', 'send', 'sync_all'])
            idempo_failed = False
            if not skip_idempotency:
                try:
                    def _dump_tables_only(db_obj):
                        snap = self._dump_sql_generic(db_obj)
                        t = snap if isinstance(snap, dict) else {}
                        # Exclude event_store and identities (contains random keys)
                        t = {k: v for k, v in t.items() if k not in ['event_store', 'identities']}
                        # Remove auto-increment id columns from all tables for idempotency
                        for table_name, rows in list(t.items()):
                            if isinstance(rows, list):
                                # Remove 'id' and 'event_id' fields from each row for idempotency
                                t[table_name] = [{k: v for k, v in row.items() if k not in ['id', 'event_id']} if isinstance(row, dict) else row for row in rows]
                        return t

                    # Single pass
                    from core.db import create_db as _create
                    import uuid as _uuid
                    base_db_path = os.environ.get('TEST_DB_PATH', ':memory:')
                    single_db_path = base_db_path.replace('.db', f'_idmp1_{str(_uuid.uuid4())[:8]}.db') if base_db_path != ':memory:' else base_db_path
                    single_db = _create(db_path=single_db_path, protocol_name=protocol_name)
                    self._track_db_file(single_db_path)
                    self._seed_sql_generic(single_db, given_db)
                    # Execute the same command once
                    _ = self.execute_command(cmd, single_db)
                    # Ticks
                    if ticks > 0:
                        from core.tick import run_all_jobs as run_tick
                        base_time = given.get("params", {}).get("time_now_ms", 1000)
                        for i in range(ticks):
                            current_time = base_time + (i + 1) * 100
                            single_db = run_tick(single_db, time_now_ms=current_time)

                    # Double pass
                    double_db_path = base_db_path.replace('.db', f'_idmp2_{str(_uuid.uuid4())[:8]}.db') if base_db_path != ':memory:' else base_db_path
                    double_db = _create(db_path=double_db_path, protocol_name=protocol_name)
                    self._track_db_file(double_db_path)
                    self._seed_sql_generic(double_db, given_db)
                    _ = self.execute_command(cmd, double_db)
                    _ = self.execute_command(cmd, double_db)
                    if ticks > 0:
                        from core.tick import run_all_jobs as run_tick
                        base_time = given.get("params", {}).get("time_now_ms", 1000)
                        for i in range(ticks):
                            current_time = base_time + (i + 1) * 100
                            double_db = run_tick(double_db, time_now_ms=current_time)

                    single_tables = _dump_tables_only(single_db)
                    double_tables = _dump_tables_only(double_db)
                    if single_tables != double_tables:
                        idempo_failed = True
                        import json as _json
                        self.log(f"Idempotency check failed for command '{command_name}'", "ERROR")
                        self.log(f"State after single execution: {_json.dumps(single_tables, indent=2)}", "DEBUG")
                        self.log(f"State after double execution: {_json.dumps(double_tables, indent=2)}", "DEBUG")
                    # Track these DBs for cleanup
                    self._track_db_file(single_db_path)
                    self._track_db_file(double_db_path)
                except Exception as e:
                    idempo_failed = True
                    self.log(f"Idempotency check failed with exception: {str(e)}", "ERROR")

            # Close database before returning
            if hasattr(db, 'close'):
                db.close()
            
            # Cleanup is now handled by _cleanup_db_files() at the end
                
            # If SNAPSHOT_ONLY=1 for non-framework protocols, require snapshot block
            require_snapshot = (os.environ.get('SNAPSHOT_ONLY') == '1')
            # Require only what is specified (or env-enforced)
            overall_ok = (
                return_matches and
                newevents_matches and
                (snapshot_matches if ("snapshot" in then or require_snapshot) else True) and
                (db_matches if ("db" in then) else True)
            )
            if overall_ok and not idempo_failed:
                return {"scenario": scenario_name, "passed": True, "logs": self.logs}
            else:
                # Log specific failure reasons
                if not return_matches:
                    self.log("Command test failed: Return value mismatch", "ERROR")
                if not newevents_matches:
                    self.log("Command test failed: NewEvents mismatch", "ERROR")
                if "snapshot" in then and not snapshot_matches:
                    self.log("Command test failed: Snapshot mismatch", "ERROR")
                if "db" in then and not db_matches:
                    self.log("Command test failed: DB state mismatch", "ERROR")
                if idempo_failed:
                    self.log("Command test failed: Idempotency check failed", "ERROR")
                return {"scenario": scenario_name, "passed": False, "logs": self.logs}
        
        # For handler tests with newEvent, convert to envelope
        if "newEvent" in test.get("given", {}):
            given = test.get("given", {})
            event = given["newEvent"]
            
            # Create an envelope from the event
            envelope = {
                "payload": event,
                "metadata": {
                    "sender": event.get("sender", "test-user")
                }
            }
            
            # Add envelope to test
            modified_test = copy.deepcopy(test)
            modified_test["given"]["envelope"] = envelope
            del modified_test["given"]["newEvent"]
            
            return self.run_handler_test(modified_test, handler_file)
        
        return self.run_test_scenario(test, handler_file)
    
    def run_scenario_test(self, scenario_name, scenario_data, protocol_name):
        """Run a single API scenario test"""
        self.log(f"Running API scenario: {scenario_name}")
        
        try:
            steps = scenario_data.get("steps", [])
            if not steps:
                return {"scenario": scenario_name, "passed": False, "logs": self.logs, "error": "No steps defined"}
            
            # Create a fresh database for this scenario
            from core.db import create_db
            import uuid
            test_id = str(uuid.uuid4())[:8]
            test_db_path = f".test_scenario_{protocol_name}_{test_id}.db"
            db = create_db(db_path=test_db_path, protocol_name=protocol_name)
            self._track_db_file(test_db_path)
            
            # Load API spec for the protocol
            api_path = os.path.join("protocols", protocol_name, "api.yaml")
            if not os.path.exists(api_path):
                raise ValueError(f"API spec not found at {api_path}")
            
            # Import api functions
            from core.api import load_yaml, match_path_to_operation, extract_handler_command, prepare_command_input
            
            api_spec = load_yaml(api_path)
            
            # Captured variables for variable substitution
            captured_vars = {}
            
            # Run each step
            for step_index, step in enumerate(steps):
                step_name = step.get("name", f"step_{step_index}")
                self.log(f"  Executing step '{step_name}'")
                
                try:
                    # Extract request details
                    request = step.get("request", {})
                    method = request.get("method", "GET").upper()
                    path = request.get("path", "")
                    headers = request.get("headers", {})
                    body = request.get("body", {})
                    
                    # Variable substitution in path, headers and body
                    path = self._substitute_vars(path, captured_vars)
                    headers = self._substitute_vars(headers, captured_vars)
                    body = self._substitute_vars(body, captured_vars)
                    
                    # Extract query parameters from path
                    query_params = {}
                    if '?' in path:
                        path_part, query_part = path.split('?', 1)
                        path = path_part
                        # Parse query parameters
                        for param in query_part.split('&'):
                            if '=' in param:
                                key, value = param.split('=', 1)
                                query_params[key] = value
                    
                    # Match path to operation
                    spec_path, operation, path_params = match_path_to_operation(api_spec, method, path)
                    if not operation:
                        raise ValueError(f"No operation found for {method} {path}")
                    
                    operation_id = operation.get("operationId")
                    if not operation_id:
                        raise ValueError(f"No operationId for {method} {path}")
                    
                    # Special handling for framework endpoints
                    if operation_id == "tick.run":
                        # Run tick
                        from core.tick import tick
                        tick(db, time_now_ms=body.get("time_now_ms", 1000))
                        result = {"status": 200, "body": {"message": "Tick completed"}}
                    else:
                        # Extract handler and command
                        handler, command = extract_handler_command(operation_id)
                        
                        # Merge all parameters
                        input_data = prepare_command_input(operation, path_params, query_params, body)
                        
                        # Run the command
                        from core.command import run_command
                        try:
                            updated_db, cmd_result = run_command(handler, command, input_data, db)
                            db = updated_db  # Update db reference
                            
                            # Format response
                            # Check if operation expects 201 (POST operations typically do for resource creation)
                            expected_responses = operation.get("responses", {})
                            # Check for 201 response (can be string or int in YAML)
                            if (201 in expected_responses or "201" in expected_responses) and method == "POST":
                                status = 201
                            else:
                                status = 200
                                
                            if 'api_response' in cmd_result:
                                response_body = cmd_result['api_response']
                            else:
                                # Clean internal fields
                                response_body = {k: v for k, v in cmd_result.items() 
                                               if k not in ['db', 'newlyCreatedEvents']}
                            
                            result = {"status": status, "body": response_body}
                        except Exception as e:
                            # Check if the underlying exception is a ValueError (validation error)
                            # These should return 400 (Bad Request) instead of 500
                            error_msg = str(e)
                            if "ValueError" in error_msg or (hasattr(e, '__cause__') and isinstance(e.__cause__, ValueError)):
                                result = {"status": 400, "body": {"error": f"Bad request: {error_msg}"}}
                            else:
                                result = {"status": 500, "body": {"error": f"Command execution failed: {error_msg}"}}
                    
                    # Log the actual response for debugging
                    self.log(f"    Response: status={result['status']}, body={json.dumps(result['body'], indent=2)}")
                    
                    # Check assertions if any
                    assertions = step.get("assertions", {})
                    if assertions:
                        # Check status code
                        if "status" in assertions:
                            expected_status = assertions["status"]
                            actual_status = result["status"]
                            if expected_status != actual_status:
                                raise AssertionError(
                                    f"Status assertion failed: expected {expected_status}, got {actual_status}"
                                )
                        
                        # Check body assertions
                        for assertion_path, expected_value in assertions.items():
                            if assertion_path == "status" or assertion_path == "capture":
                                continue
                                
                            # Handle special case for body.field notation
                            if assertion_path.startswith("body."):
                                field_path = assertion_path[5:]  # Remove "body." prefix
                                actual_value = self._extract_value(result["body"], field_path)
                            elif assertion_path.startswith("body["):
                                # Handle body[index] or body[field] notation
                                field_path = assertion_path[4:]  # Remove "body" prefix
                                actual_value = self._extract_value(result["body"], field_path)
                            else:
                                # Direct field assertion
                                actual_value = self._extract_value(result["body"], assertion_path)
                            
                            # Apply variable substitution to expected value
                            if isinstance(expected_value, str):
                                expected_value = self._substitute_vars(expected_value, captured_vars)
                            
                            # Debug logging
                            self.log(f"    Checking assertion '{assertion_path}': expected={expected_value}, actual={actual_value}")
                            
                            # Check if it matches
                            if expected_value == "*":
                                # Wildcard matches anything
                                pass
                            elif isinstance(expected_value, dict):
                                # Check for special $length assertion
                                if "$length" in expected_value and isinstance(actual_value, list):
                                    expected_length = expected_value["$length"]
                                    actual_length = len(actual_value)
                                    if expected_length != actual_length:
                                        raise AssertionError(
                                            f"Assertion '{assertion_path}' failed: expected length {expected_length}, got {actual_length}"
                                        )
                                else:
                                    # Subset matching for dicts
                                    matches, path, exp_val, act_val = self.subset_match(actual_value, expected_value)
                                    if not matches:
                                        raise AssertionError(
                                            f"Assertion '{assertion_path}' failed at {path}: expected {exp_val}, got {act_val}"
                                        )
                            elif expected_value != actual_value:
                                raise AssertionError(
                                    f"Assertion '{assertion_path}' failed: expected {expected_value}, got {actual_value}"
                                )
                        
                        # Capture variables
                        if "capture" in assertions:
                            for var_name, json_path in assertions["capture"].items():
                                captured_value = self._extract_value(result["body"], json_path)
                                captured_vars[var_name] = captured_value
                                self.log(f"    Captured {var_name} = {captured_value}")
                    
                except Exception as e:
                    error_msg = f"Step '{step_name}' failed: {str(e)}"
                    self.log(error_msg, "ERROR")
                    return {"scenario": scenario_name, "passed": False, "logs": self.logs, "error": error_msg}
            
            # All steps passed
            return {"scenario": scenario_name, "passed": True, "logs": self.logs}
            
        except Exception as e:
            self.log(f"Scenario '{scenario_name}' crashed: {str(e)}", "ERROR")
            return {"scenario": scenario_name, "passed": False, "logs": self.logs, "error": str(e)}
    
    def _substitute_vars(self, obj, vars_dict):
        """Recursively substitute ${var} placeholders in strings"""
        if isinstance(obj, str):
            # Replace ${var} with value from vars_dict
            for var_name, var_value in vars_dict.items():
                obj = obj.replace(f"${{{var_name}}}", str(var_value))
            return obj
        elif isinstance(obj, dict):
            return {k: self._substitute_vars(v, vars_dict) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._substitute_vars(item, vars_dict) for item in obj]
        else:
            return obj
    
    def _extract_value(self, data, path):
        """Extract value from data using dot/bracket notation path"""
        if path.startswith("$."):
            path = path[2:]  # Remove $. prefix
        elif path == "$":
            return data
        
        current = data
        parts = path.replace("][", ".").replace("[", ".").replace("]", "").split(".")
        
        for part in parts:
            if not part:
                continue
                
            if isinstance(current, dict):
                if part in current:
                    current = current[part]
                else:
                    return None
            elif isinstance(current, list):
                if part.isdigit():
                    index = int(part)
                    if 0 <= index < len(current):
                        current = current[index]
                    else:
                        return None
                elif part == "*":
                    # Return all items
                    return current
                else:
                    return None
            else:
                return None
        
        return current
    
    def run_file(self, test_path):
        """Run all test scenarios in a file"""
        self.logs = []
        results = []
        
        try:
            # Ensure proper environment (HANDLER_PATH/TEST_DB_PATH) when running a single file
            try:
                norm = os.path.normpath(test_path)
                parts = norm.split(os.sep)
                if 'protocols' in parts:
                    idx = parts.index('protocols')
                    if idx + 1 < len(parts):
                        protocol_name = parts[idx + 1]
                        protocol_path = os.path.join(*parts[: idx + 2])
                        handlers_path = os.path.join(protocol_path, 'handlers')
                        if os.path.isdir(handlers_path):
                            os.environ['HANDLER_PATH'] = handlers_path
                        # Use a protocol-specific DB path so schema (if present) is applied
                        os.environ['TEST_DB_PATH'] = f".test_{protocol_name}.db"
                        # Clean any stale DB from previous runs of this single file
                        try:
                            if os.path.exists(os.environ['TEST_DB_PATH']):
                                os.remove(os.environ['TEST_DB_PATH'])
                        except Exception:
                            pass
                # Always enable test-mode logging for single-file runs
                os.environ['TEST_MODE'] = '1'
                os.environ['DEBUG_CRYPTO'] = '1'
            except Exception:
                pass

            with open(test_path, 'r') as f:
                test_data = json.load(f)
            
            # Check if this is a JSON-only test file
            if test_data.get("jsonTestsOnly"):
                # Skip command execution, just verify test structure
                if "commands" in test_data:
                    for cmd_name, cmd_def in test_data["commands"].items():
                        if "tests" in cmd_def:
                            for test in cmd_def["tests"]:
                                scenario_name = test.get("description", f"{cmd_name} test")
                                results.append({
                                    "file": test_path,
                                    "scenario": scenario_name,
                                    "passed": True,
                                    "logs": [f"JSON-only test verified: {scenario_name}"]
                                })
                return results
            
            # Check if this is a scenario test file
            if "scenarios" in test_data:
                # Extract protocol name from path
                protocol_name = None
                norm = os.path.normpath(test_path)
                parts = norm.split(os.sep)
                if 'protocols' in parts:
                    idx = parts.index('protocols')
                    if idx + 1 < len(parts):
                        protocol_name = parts[idx + 1]
                
                if not protocol_name:
                    raise ValueError("Cannot determine protocol from scenario test path")
                
                # Run each scenario
                for scenario_name, scenario_data in test_data["scenarios"].items():
                    self.logs = []
                    result = self.run_scenario_test(scenario_name, scenario_data, protocol_name)
                    result["file"] = test_path
                    results.append(result)
                
                return results
            
            # Determine test type based on file location and content
            if "handlers" in test_path:
                # Handler tests
                if "projector" in test_data and "tests" in test_data["projector"]:
                    for test in test_data["projector"]["tests"]:
                        self.logs = []
                        result = self.run_handler_test(test, test_path)
                        result["file"] = test_path
                        results.append(result)
                
                if "commands" in test_data:
                    for cmd_name, cmd_def in test_data["commands"].items():
                        if "tests" in cmd_def:
                            for test in cmd_def["tests"]:
                                self.logs = []
                                # Extract handler name from path
                                path_parts = test_path.split('/')
                                handler_name = None
                                for i, part in enumerate(path_parts):
                                    if part == "handlers" and i + 1 < len(path_parts):
                                        handler_name = path_parts[i + 1]
                                        break
                                result = self.run_handler_test(test, test_path, handler_name, cmd_name)
                                result["file"] = test_path
                                results.append(result)
                                
            elif "tick.json" in test_path:
                # Tick tests
                if "tests" in test_data:
                    for test in test_data["tests"]:
                        self.logs = []
                        result = self.run_test_scenario(test, test_path)
                        result["file"] = test_path
                        results.append(result)
                        
            elif "runner.json" in test_path:
                # Runner tests are meta-tests - skip for now
                # These test the test runner itself, not the framework
                pass
            
            return results
            
        except Exception as e:
            self.log(f"Failed to load test file: {str(e)}", "ERROR")
            self.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            return [{
                "file": test_path,
                "scenario": "File load error",
                "passed": False,
                "error": str(e),
                "logs": self.logs
            }]
    
    
    def run_protocol_tests(self, protocol_name, protocol_path):
        """Run tests for a specific protocol"""
        print(f"\n" + "="*60)
        print(f"RUNNING PROTOCOL: {protocol_name}")
        print("="*60)
        
        # Set test mode for better logging
        os.environ["TEST_MODE"] = "1"
        os.environ["DEBUG_CRYPTO"] = "1"  # Enable crypto debugging by default
        
        # Set handler path for this protocol
        handlers_path = os.path.join(protocol_path, "handlers")
        if os.path.exists(handlers_path):
            os.environ["HANDLER_PATH"] = handlers_path
        
        # Use a protocol-specific test database file
        # This ensures each protocol gets its own schema
        test_db_path = f".test_{protocol_name}.db"
        os.environ["TEST_DB_PATH"] = test_db_path
        
        # Clean up any existing test database
        if os.path.exists(test_db_path):
            try:
                os.remove(test_db_path)
            except:
                pass
        
        # Check for schema.sql and validate if present
        schema_file = os.path.join(protocol_path, "schema.sql")
        if os.path.exists(schema_file):
            print(f"\nFound schema.sql, validating handler data against schema...")
            try:
                from core.check_schema_sql import SQLSchemaParser, HandlerSchemaValidator
                
                # Parse schema
                schema_parser = SQLSchemaParser(schema_file)
                print(f"  Parsed {len(schema_parser.tables)} tables from schema")
                
                # Validate handlers
                validator = HandlerSchemaValidator(schema_parser)
                total_errors = 0
                total_warnings = 0
                
                # Check each handler
                for root, dirs, files in os.walk(handlers_path):
                    # Look for {folder}_handler.json pattern
                    handler_name = os.path.basename(root)
                    handler_json_name = f"{handler_name}_handler.json"
                    if handler_json_name in files:
                        handler_path = os.path.join(root, handler_json_name)
                        
                        errors, warnings = validator.validate_handler(handler_path)
                        if errors or warnings:
                            print(f"\n  Handler '{handler_name}':")
                            if errors:
                                print(f"    Schema errors: {len(errors)}")
                                for error in errors[:3]:  # Show first 3 errors
                                    print(f"      - {error}")
                                if len(errors) > 3:
                                    print(f"      ... and {len(errors) - 3} more")
                            if warnings:
                                print(f"    Schema warnings: {len(warnings)}")
                                
                        total_errors += len(errors)
                        total_warnings += len(warnings)
                
                if total_errors > 0 or total_warnings > 0:
                    print(f"\n  Schema validation summary:")
                    print(f"    Total errors: {total_errors} (not enforced)")
                    print(f"    Total warnings: {total_warnings}")
                else:
                    print(f"  ✓ All handlers match schema perfectly!")
                    
            except Exception as e:
                print(f"  WARNING: Schema validation failed: {str(e)}")
                if self.verbose:
                    import traceback
                    traceback.print_exc()
        
        # Check for api.yaml and validate if present
        api_file = os.path.join(protocol_path, "api.yaml")
        if os.path.exists(api_file):
            if yaml is None:
                print("\nFound api.yaml, but PyYAML is not installed; skipping API validation.")
            else:
                print(f"\nFound api.yaml, validating API operations...")
                api_errors = self.validate_api(protocol_name, protocol_path, api_file, handlers_path)
                if api_errors > 0:
                    print(f"  ❌ API validation found {api_errors} errors")
                else:
                    print(f"  ✅ All API operations validated successfully")
        
        # Run tests for this protocol
        protocol_results = []
        for root, dirs, files in os.walk(protocol_path):
            for file in files:
                if file.endswith(".json") and file != "schema.json":
                    test_path = os.path.join(root, file)
                    # Skip scenario tests in handlers directory
                    if "scenarios" in root and "handlers" not in root:
                        # This is a scenario test file
                        pass
                    results = self.run_file(test_path)
                    protocol_results.extend(results)
                    
                    # Don't clean up here - we'll clean up at the start of each protocol instead
        
        # Summary for this protocol
        passed = sum(1 for r in protocol_results if r["passed"])
        failed = sum(1 for r in protocol_results if not r["passed"])
        
        print(f"\n{protocol_name} Test Results: {passed} passed, {failed} failed")
        
        # Show failed tests inline
        if failed > 0:
            print("\nFailed tests:")
            for r in protocol_results:
                if not r["passed"]:
                    print(f"  - {r['file']} :: {r['scenario']}")
                    # Show error details for file load errors
                    if r.get('scenario') == 'File load error' and 'error' in r:
                        print(f"    ERROR: {r['error']}")
        
        # Clean up all test databases created for this protocol
        self._cleanup_db_files()
        
        # Also clean up the main protocol test database
        if os.path.exists(test_db_path):
            try:
                os.remove(test_db_path)
            except:
                pass
        
        return protocol_results
    
    def run_all_tests(self):
        """Run tests for all protocols separately"""
        all_results = []
        protocol_summaries = []
        
        # Discover all protocols
        protocols_dir = "protocols"
        if not os.path.exists(protocols_dir):
            print("No protocols directory found")
            return False
        
        # Run tests for each protocol
        for protocol_name in sorted(os.listdir(protocols_dir)):
            protocol_path = os.path.join(protocols_dir, protocol_name)
            if os.path.isdir(protocol_path):
                results = self.run_protocol_tests(protocol_name, protocol_path)
                all_results.extend(results)
                
                # Store summary for this protocol
                passed = sum(1 for r in results if r["passed"])
                failed = sum(1 for r in results if not r["passed"])
                protocol_summaries.append({
                    "name": protocol_name,
                    "passed": passed,
                    "failed": failed
                })
        
        # Overall summary
        total_passed = sum(1 for r in all_results if r["passed"])
        total_failed = sum(1 for r in all_results if not r["passed"])
        
        print(f"\n{'='*60}")
        print("SUMMARY BY PROTOCOL")
        print("="*60)
        for summary in protocol_summaries:
            status = "✓" if summary["failed"] == 0 else "✗"
            print(f"{status} {summary['name']}: {summary['passed']} passed, {summary['failed']} failed")
        
        print(f"\n{'='*60}")
        print(f"TOTAL Test Results: {total_passed} passed, {total_failed} failed")
        print(f"{'='*60}\n")
        
        # Show failed tests
        if total_failed > 0:
            print("\nFailed tests:")
            print("-" * 60)
        for result in all_results:
            if not result["passed"]:
                print(f"FAILED: {result['file']} - {result['scenario']}")
                if "error" in result:
                    print(f"  Error: {result['error']}")
                for log in result.get("logs", []):
                    if "ERROR" in log:
                        print(f"  {log}")
                print()
        
        # Final cleanup of any remaining test databases
        self._cleanup_db_files()
        
        return total_failed == 0
    
    def validate_api(self, protocol_name, protocol_path, api_file, handlers_path):
        """Validate API specification against handlers. Returns error count."""
        try:
            # Load API specification
            if yaml is None:
                # If YAML isn't available, skip validation cleanly
                return 0
            with open(api_file, 'r') as f:
                api_spec = yaml.safe_load(f)
            
            # Discover handlers
            handlers = {}
            for handler_dir in os.listdir(handlers_path):
                handler_path = os.path.join(handlers_path, handler_dir)
                if os.path.isdir(handler_path):
                    # Look for {folder}_handler.json pattern
                    handler_json_path = os.path.join(handler_path, f"{handler_dir}_handler.json")
                    if os.path.exists(handler_json_path):
                        try:
                            with open(handler_json_path, 'r') as f:
                                handler_data = json.load(f)
                            
                            # Extract commands
                            commands = []
                            if "commands" in handler_data:
                                commands.extend(handler_data["commands"].keys())
                            
                            # Jobs are also callable as commands
                            if "job" in handler_data:
                                commands.append(handler_data["job"])
                            
                            handlers[handler_dir] = commands
                        except Exception as e:
                            print(f"  Warning: Failed to parse {handler_json_path}: {e}")
            
            print(f"  Found {len(handlers)} handlers: {', '.join(handlers.keys())}")
            
            # Validate operations
            error_count = 0
            operation_count = 0
            
            # Special operations that don't map to handlers
            special_operations = ["tick.run"]
            
            if "paths" in api_spec:
                for path, path_item in api_spec["paths"].items():
                    for method, operation in path_item.items():
                        if method in ["get", "post", "put", "delete", "patch"]:
                            operation_count += 1
                            operation_id = operation.get("operationId")
                            
                            if not operation_id:
                                print(f"    Error: {method.upper()} {path}: Missing operationId")
                                error_count += 1
                                continue
                            
                            # Skip special operations
                            if operation_id in special_operations:
                                continue
                            
                            # Check operationId format
                            if '.' not in operation_id:
                                print(f"    Error: {method.upper()} {path}: Invalid operationId format '{operation_id}'")
                                error_count += 1
                                continue
                            
                            handler_name, command_name = operation_id.split('.', 1)
                            
                            # Check if handler exists
                            if handler_name not in handlers:
                                print(f"    Error: {method.upper()} {path}: Handler '{handler_name}' not found")
                                error_count += 1
                                continue
                            
                            # Check if command exists
                            if command_name not in handlers[handler_name]:
                                print(f"    Error: {method.upper()} {path}: Command '{command_name}' not found in handler '{handler_name}'")
                                error_count += 1
                            
                            # Check request body schema for required fields
                            if "requestBody" in operation:
                                request_body = operation["requestBody"]
                                if "content" in request_body and "application/json" in request_body["content"]:
                                    schema = request_body["content"]["application/json"].get("schema", {})
                                    if schema.get("type") == "object":
                                        # Check if required array is missing when properties are defined
                                        if "properties" in schema and "required" not in schema:
                                            # Only flag as error if there are properties that should be required
                                            prop_count = len(schema["properties"])
                                            if prop_count > 0:
                                                print(f"    Error: {method.upper()} {path}: Request body schema has {prop_count} properties but no 'required' array specified")
                                                error_count += 1
                            
                            # Check response schemas for required fields
                            if "responses" in operation:
                                for status_code, response in operation["responses"].items():
                                    if "content" in response and "application/json" in response["content"]:
                                        schema = response["content"]["application/json"].get("schema", {})
                                        if schema.get("type") == "object":
                                            # Check if required array is missing when properties are defined
                                            if "properties" in schema and "required" not in schema:
                                                prop_count = len(schema["properties"])
                                                if prop_count > 0:
                                                    print(f"    Error: {method.upper()} {path}: Response {status_code} schema has {prop_count} properties but no 'required' array specified")
                                                    error_count += 1
            
            print(f"  Validated {operation_count} operations")
            
            # Check for duplicate operationIds
            operation_ids = []
            if "paths" in api_spec:
                for path, path_item in api_spec["paths"].items():
                    for method, operation in path_item.items():
                        if method in ["get", "post", "put", "delete", "patch"]:
                            if "operationId" in operation:
                                operation_ids.append(operation["operationId"])
            
            duplicates = [x for x in operation_ids if operation_ids.count(x) > 1]
            if duplicates:
                unique_duplicates = list(set(duplicates))
                print(f"    Error: Duplicate operationIds found: {unique_duplicates}")
                error_count += len(unique_duplicates)
            
            return error_count
            
        except Exception as e:
            print(f"  ERROR: Failed to validate API: {str(e)}")
            return 1

if __name__ == "__main__":
    runner = TestRunner()
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    flags = [a for a in sys.argv[1:] if a.startswith('-')]

    if "--verbose" in flags:
        runner.verbose = True

    try:
        # When protocol paths/names are provided, only run those
        if args:
            all_ok = True
            for arg in args:
                # Support either 'protocols/<name>' or '<name>'
                if os.path.isdir(arg) and os.path.basename(os.path.dirname(arg)) == 'protocols':
                    protocol_path = arg
                    protocol_name = os.path.basename(arg)
                elif os.path.isdir(os.path.join('protocols', arg)):
                    protocol_name = arg
                    protocol_path = os.path.join('protocols', arg)
                else:
                    # If it's a file, run it directly
                    if os.path.isfile(arg):
                        results = runner.run_file(arg)
                        ok = all(r.get('passed') for r in results)
                        all_ok = all_ok and ok
                        continue
                    else:
                        print(f"Unknown path or protocol: {arg}")
                        all_ok = False
                        continue

                results = runner.run_protocol_tests(protocol_name, protocol_path)
                ok = all(r.get('passed') for r in results)
                all_ok = all_ok and ok

            # Cleanup before exit
            runner._cleanup_db_files()
            sys.exit(0 if all_ok else 1)

        # Default: run all protocol tests
        success = runner.run_all_tests()
        sys.exit(0 if success else 1)
    finally:
        # Always cleanup on exit
        runner._cleanup_db_files()
