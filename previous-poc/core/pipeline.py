"""
Production pipeline runner for processing envelopes through handlers.
Supports verbose logging and database inspection.
"""
import importlib
import json
import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Any, Optional

from .db import get_connection, init_database
from .handlers import registry


class PipelineRunner:
    """Production pipeline runner with configurable logging."""
    
    def __init__(self, db_path: str = "quiet.db", verbose: bool = False, root_secret: str | bytes | None = None):
        self.db_path = db_path
        self.verbose = verbose
        self.processed_count = 0
        self.emitted_count = 0
        self.start_time = time.time()
        # If provided, seed the process-local root secret for local-only encryption
        try:
            if root_secret is not None:
                from . import crypto as _crypto
                _crypto.set_root_secret(root_secret)
        except Exception:
            # Non-fatal; handlers will fallback to env/default
            pass
        
    def log(self, message: str) -> None:
        """Log message with timestamp."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"[{timestamp}] {message}")
        
    def log_envelope(self, action: str, handler: str, envelope: Dict[str, Any]) -> None:
        """Log envelope details in verbose mode."""
        if self.verbose:
            # Format envelope for readability, converting bytes to hex
            def serialize_envelope(obj: Any) -> Any:
                if isinstance(obj, bytes):
                    return f"<bytes:{len(obj)}:{obj[:20].hex()}...>" if len(obj) > 20 else obj.hex()
                elif isinstance(obj, dict):
                    return {k: serialize_envelope(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [serialize_envelope(v) for v in obj]
                return obj
                
            serializable = serialize_envelope(envelope)
            envelope_str = json.dumps(serializable, indent=2)
            # Truncate large fields
            if len(envelope_str) > 500:
                envelope_str = envelope_str[:500] + "..."
            self.log(f"{action} by {handler}:\n{envelope_str}")
            
    def run(self, protocol_dir: str, input_envelopes: Optional[List[dict[str, Any]]] = None, commands: Optional[List[Dict[str, Any]]] = None, db: Optional[sqlite3.Connection] = None) -> Dict[str, str]:
        """Run the pipeline with given protocol and optional input envelopes or commands.

        Commands should be a list of dicts with 'name' and 'params' keys.
        Returns mapping of event_type -> event_id for events that were stored (one per type).
        """
        self.log(f"Starting pipeline runner")
        self.log(f"Database: {self.db_path}")
        self.log(f"Protocol: {protocol_dir}")

        # Initialize database if not provided
        close_db = False
        if db is None:
            db = get_connection(self.db_path)
            close_db = True
        init_database(db, protocol_dir)

        # Load protocol handlers
        self._load_protocol_handlers(protocol_dir)

        # Track stored events for return value
        stored_events = {}

        # Deprecated: 'commands' parameter no longer supported; flows emit directly

        # Process input envelopes if provided
        if input_envelopes:
            self.log(f"Processing {len(input_envelopes)} input envelopes")
            stored = self._process_envelopes(input_envelopes, db)
            stored_events.update(stored)

        # Check for any envelopes in outgoing_queue (not used by default)

        # Summary
        elapsed = time.time() - self.start_time
        self.log(f"Pipeline complete in {elapsed:.2f}s")
        self.log(f"Envelopes processed: {self.processed_count}")
        self.log(f"Envelopes emitted: {self.emitted_count}")

        if close_db:
            db.close()

        return stored_events
        
    def _load_protocol_handlers(self, protocol_dir: str) -> None:
        """Dynamically load all handlers from a protocol."""
        import importlib
        import os
        
        protocol_name = Path(protocol_dir).name
        self.log(f"Loading handlers for protocol: {protocol_name}")
        
        # Commands registry removed; flows register via @flow_op on import
        
        handlers_dir = Path(protocol_dir) / "handlers"
        if not handlers_dir.exists():
            self.log(f"No handlers directory found in {protocol_dir}")
            return
            
        # Deterministic but arbitrary order: load handlers sorted by name.
        # Handlers' own filters determine processing flow.
        items = sorted(handlers_dir.iterdir(), key=lambda p: p.name)

        # Import handler modules - supports both subdirectory and flat structure
        for item in items:
            if item.name.startswith("_"):
                continue
                
            # Check for subdirectory structure (old format)
            if item.is_dir():
                handler_file = item / "handler.py"
                if handler_file.exists():
                    module_name = f"protocols.{protocol_name}.handlers.{item.name}.handler"
                    self._load_handler_module(module_name, item.name)
                    
            # Skip *_handler.py wrapper files - we're loading directly from main files now
            elif item.is_file() and item.name.endswith('_handler.py'):
                continue  # Skip wrapper files

            # Load regular .py handler files (excluding __init__ and test files)
            elif item.is_file() and item.name.endswith('.py') and item.name != '__init__.py' and not item.name.startswith('test_'):
                handler_name = item.stem
                module_name = f"protocols.{protocol_name}.handlers.{handler_name}"
                self.log(f"Checking module: {module_name}")
                self._load_handler_module(module_name, handler_name)
                
    def _load_handler_module(self, module_name: str, handler_name: str) -> None:
        """Load a handler module and register handler classes."""
        try:
            module = importlib.import_module(module_name)
            # Look for handler classes
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (hasattr(attr, '__bases__') and 
                    any('Handler' in str(base) for base in attr.__bases__)):
                    handler_instance = attr()
                    registry.register(handler_instance)
                    self.log(f"Registered handler: {handler_instance.name}")
        except Exception as e:
            self.log(f"Failed to load handler {handler_name}: {e}")
                
    def _process_envelopes(self, envelopes: List[dict[str, Any]], db: sqlite3.Connection) -> Dict[str, str]:
        """Process a batch of envelopes through the pipeline.

        Returns mapping of event_type -> event_id for events that were stored (one per type).
        """
        # Convert hex strings to bytes if needed
        processed_envelopes = []
        for env in envelopes:
            env_copy = env.copy()
            if 'raw_data' in env_copy and isinstance(env_copy['raw_data'], str):
                try:
                    env_copy['raw_data'] = bytes.fromhex(env_copy['raw_data'])
                except ValueError:
                    self.log(f"Warning: Invalid hex in raw_data, keeping as string")
            processed_envelopes.append(env_copy)

        # Track all envelopes we process (for tracking stored events)
        all_processed = []

        # Track generated event IDs for reference (one per type)
        generated_ids: Dict[str, List[str]] = {}

        # Process all events sequentially (no placeholder semantics)
        queue = processed_envelopes
        iterations = 0

        # Max times a single envelope can be processed
        max_envelope_processes = 100

        # Track total envelopes processed across all iterations for diagnostics
        total_envelopes_processed = 0

        while queue:
            iterations += 1
            total_envelopes_processed += len(queue)

            if self.verbose:
                self.log(f"--- Iteration {iterations} with {len(queue)} envelopes ---")

            next_queue = []
            for envelope in queue:
                # Use a simple accumulator field to track processing count
                process_count = envelope.get('_process_count', 0) + 1
                envelope['_process_count'] = process_count

                # No normalization of legacy flags here; flows must set is_outgoing

                # Check if this envelope has been processed too many times
                if process_count > max_envelope_processes:
                    # For debugging, generate a simple identifier based on event type and current envelope id
                    debug_id = f"{envelope.get('event_type', 'unknown')}_{envelope.get('event_id', 'no_id')}"
                    if envelope.get('event_type') == 'user':
                        self.log(f"ERROR: User envelope loop! event_id={envelope.get('event_id')}, stored={envelope.get('stored')}, validated={envelope.get('validated')}")
                    self.log(f"ERROR: Envelope loop detected! {debug_id} processed {process_count} times")
                    self.log(f"ERROR: Dropping envelope of type {envelope.get('event_type', 'unknown')}")
                    continue  # Skip this envelope

                self.processed_count += 1

                # Process through all matching handlers
                # The handlers modify the envelope in-place
                emitted = registry.process_envelope(envelope, db)

                # Track generated event_id for placeholder resolution
                if 'event_id' in envelope:
                    event_type = envelope.get('event_type', '')
                    if event_type:
                        if event_type not in generated_ids:
                            generated_ids[event_type] = []
                        generated_ids[event_type].append(envelope['event_id'])

                # Track the processed envelope (handlers may have modified it)
                all_processed.append(envelope)
                if envelope.get('event_type') == 'user' and envelope.get('event_id'):
                    print(f"[pipeline] User event {envelope.get('event_id')} added to all_processed, request_id={envelope.get('request_id')}")

                # Normalize emitted to always be a flat list of envelopes
                # Also propagate request_id to all emitted envelopes to maintain linkage
                normalized_emitted = []
                parent_req = envelope.get('request_id')
                for item in emitted:
                    if isinstance(item, dict):
                        # Single envelope
                        if parent_req and 'request_id' not in item:
                            item['request_id'] = parent_req
                        normalized_emitted.append(item)
                    elif isinstance(item, list):
                        # List of envelopes (shouldn't happen but handle it)
                        for subitem in item:
                            if isinstance(subitem, dict):
                                if parent_req and 'request_id' not in subitem:
                                    subitem['request_id'] = parent_req
                                normalized_emitted.append(subitem)
                            else:
                                self.log(f"WARNING: Skipping non-dict item in emitted: {type(subitem)}")
                    else:
                        self.log(f"WARNING: Skipping non-dict/list item in emitted: {type(item)}")

                if self.verbose and normalized_emitted:
                    for handler in registry._handlers:
                        if handler.filter(envelope):
                            self.log_envelope("CONSUMED", handler.name, envelope)
                            for e in normalized_emitted:
                                self.log_envelope("EMITTED", handler.name, e)

                # Add emitted envelopes to next queue
                next_queue.extend(normalized_emitted)
                self.emitted_count += len(emitted)

            queue = next_queue

        # No placeholder pass

        # Track stored events (only return one per type)
        # Use a dict to track unique events by event_id to avoid counting duplicates
        stored_events_by_id = {}

        # Debug: count events in all_processed
        user_count = sum(1 for e in all_processed if e.get('event_type') == 'user')
        user_with_id = sum(1 for e in all_processed if e.get('event_type') == 'user' and e.get('event_id'))
        network_count = sum(1 for e in all_processed if e.get('event_type') == 'network')
        network_with_id = sum(1 for e in all_processed if e.get('event_type') == 'network' and e.get('event_id'))
        group_count = sum(1 for e in all_processed if e.get('event_type') == 'group')
        group_with_id = sum(1 for e in all_processed if e.get('event_type') == 'group' and e.get('event_id'))
        if user_count > 0 or network_count > 0 or group_count > 0:
            print(f"[pipeline] Events processed - user: {user_count}/{user_with_id}, network: {network_count}/{network_with_id}, group: {group_count}/{group_with_id}")

        for envelope in all_processed:
            # Consider envelopes tied to this request
            if 'request_id' not in envelope:
                if envelope.get('event_type') == 'user' and envelope.get('event_id'):
                    print(f"[pipeline] User event {envelope.get('event_id')} skipped - no request_id!")
                continue

            event_type = envelope.get('event_type')
            event_id = envelope.get('event_id')

            if not event_type or not event_id:
                if event_type == 'user':
                    print(f"[pipeline] User event skipped - event_type={event_type}, event_id={event_id}, stored={envelope.get('stored')}")
                continue

            # Treat as "stored" if:
            # - event_store stored it (stored==True), or
            # - it's a local-only peer_secret projected into identities (projected==True and event_type=='peer_secret')
            is_effectively_stored = (
                envelope.get('stored') is True or
                (event_type in ('peer_secret', 'peer') and envelope.get('projected') is True)
            )

            if is_effectively_stored:
                # Only track each unique event_id once (deduplication)
                stored_events_by_id[event_id] = event_type
                # Debug logging removed - deduplication handles multiple passes

        # Count unique events per type
        event_counts: Dict[str, int] = {}
        event_ids: Dict[str, str] = {}
        for event_id, event_type in stored_events_by_id.items():
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            # Store first ID for each type
            if event_type not in event_ids:
                event_ids[event_type] = event_id

        # Return all event IDs where there's exactly one event of that type
        # This allows commands that create multiple different event types to return all IDs
        stored_ids: Dict[str, str] = {}
        for event_type, count in event_counts.items():
            if count == 1:
                stored_ids[event_type] = event_ids[event_type]
                if event_type == 'user':
                    print(f"[pipeline] Returning user event_id={event_ids[event_type]} (count={count})")

        # Fallback: if nothing was effectively stored, but a single ID was generated for a type,
        # return that generated ID. This covers projected-only lifecycle events like 'peer'.
        if not stored_ids:
            for etype, ids in generated_ids.items():
                if len(ids) == 1 and etype not in stored_ids:
                    stored_ids[etype] = ids[0]

        return stored_ids

    # Placeholder resolution removed: flows emit sequentially and provide real IDs.

    def _process_outgoing_queue(self, db: sqlite3.Connection) -> None:
        """Process any envelopes in the outgoing queue."""
        cursor = db.execute("""
            SELECT id, envelope_data 
            FROM outgoing_queue 
            WHERE due_ms <= ?
            ORDER BY due_ms
            LIMIT 1000
        """, (int(time.time() * 1000),))
        
        rows = cursor.fetchall()
        if rows:
            self.log(f"Processing {len(rows)} envelopes from outgoing queue")
            
            envelopes = []
            ids_to_delete = []
            
            for row in rows:
                try:
                    envelope = json.loads(row['envelope_data'])
                    envelopes.append(envelope)
                    ids_to_delete.append(row['id'])
                except json.JSONDecodeError:
                    self.log(f"Failed to parse envelope {row['id']} from queue")
                    
            # Process the envelopes
            if envelopes:
                self._process_envelopes(envelopes, db)
                
            # Delete processed envelopes from queue
            if ids_to_delete:
                placeholders = ','.join('?' * len(ids_to_delete))
                db.execute(f"DELETE FROM outgoing_queue WHERE id IN ({placeholders})", ids_to_delete)
                db.commit()
                
    def dump_database(self) -> None:
        """Dump all tables from the database."""
        from .queries import dump_database as dump_db_query
        import json

        self.log(f"\n=== DATABASE DUMP: {self.db_path} ===")

        db = get_connection(self.db_path)

        try:
            # Use the query function to get all table data
            from .db import get_readonly_connection
            readonly_db = get_readonly_connection(db)
            result = dump_db_query(readonly_db, {})

            # Just dump as pretty JSON
            print(json.dumps(result, indent=2, sort_keys=True))
        finally:
            db.close()
            print("\n=== END DATABASE DUMP ===")
