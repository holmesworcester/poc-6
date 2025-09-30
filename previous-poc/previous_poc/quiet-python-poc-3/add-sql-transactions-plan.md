# SQL Transactions Implementation Plan

## Overview
This plan implements SQL transaction management for the event-driven framework, ensuring atomic event processing with proper rollback on errors. The design distinguishes between domain state (managed through events) and infrastructure state (queues that can be modified directly).

## Current State Analysis

### Problems Identified
1. **No transaction boundaries**: Each db write commits immediately
2. **No rollback capability**: If a projector fails halfway, partial state remains
3. **Inefficient batch processing**: Tick operations process events one at a time
4. **No write serialization**: Concurrent writes could corrupt state

### Current State
- Commands directly modify infrastructure queues (`state.outgoing`, `incoming`)
- All state changes auto-commit immediately
- No support for batch operations during tick processing
- No protection against concurrent modifications

## Proposed Solution

### Key Architectural Decision: Infrastructure vs Domain State

We distinguish between two types of state:
1. **Domain State**: Business events that should go through projectors (e.g., message created, identity joined)
2. **Infrastructure State**: Transport/queue operations that can be modified directly (e.g., outgoing/incoming queues)

This acknowledges that not everything needs to be an event - transport queues are operational infrastructure, not domain events.

### Formalizing Infrastructure State

Currently, protocols use these infrastructure queues:
- `state.outgoing` - Queue of messages waiting to be sent
- `incoming` - Queue of received messages to process (top-level, not in state)

Error handling uses ephemeral logging instead of persisting errors in the database.

For now, we can hardcode the protocol infrastructure state:

```python
# core/constants.py
INFRASTRUCTURE_PATHS = [
    'state.outgoing',
    'incoming',  # Top-level incoming queue used by protocols
]
```

If future protocols need custom infrastructure state, we can add a `protocol.yaml` configuration file at that time.

### Phase 1: Add Transaction Support to PersistentDict

```python
# core/db.py modifications
class PersistentDict:
    def __init__(self, ...):
        # ... existing init ...
        # Set SERIALIZABLE isolation for full serialization
        self._conn.execute("PRAGMA read_uncommitted = 0")
        self._conn.isolation_level = 'EXCLUSIVE'  # SQLite's strongest isolation
        
    def begin_transaction(self):
        """Start a new transaction, disable auto-commit"""
        self._in_transaction = True
        self._transaction_cache = {}
        self._conn.execute("BEGIN EXCLUSIVE")  # Lock database for writes
        
    def commit(self):
        """Commit all pending changes"""
        if not self._in_transaction:
            return
            
        try:
            # Apply all changes from transaction cache
            for key, value in self._transaction_cache.items():
                self._commit_change(key, value)
            
            self._conn.commit()
            self._cache.update(self._transaction_cache)
        finally:
            self._transaction_cache = {}
            self._in_transaction = False
        
    def rollback(self):
        """Discard all pending changes"""
        if not self._in_transaction:
            return
            
        try:
            self._conn.rollback()
        finally:
            self._transaction_cache = {}
            self._in_transaction = False
        
    def __setitem__(self, key, value):
        if self._in_transaction:
            # Store in transaction cache instead of committing
            self._transaction_cache[key] = value
        else:
            # Current behavior: immediate commit
            self._commit_change(key, value)
    
    def with_retry(self, func, max_retries=3, timeout_ms=30000):
        """Execute function with timeout and retry logic"""
        import sqlite3
        
        for attempt in range(max_retries):
            try:
                # Set busy timeout for SQLite
                self._conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
                return func()
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))  # Exponential backoff
                    continue
                raise
```

### Phase 2: Transaction Management for Event Processing

```python
# core/handle.py modifications
def handle(db, envelope, time_now_ms, auto_transaction=True):
    """Process a single event, optionally within a transaction"""
    
    if auto_transaction:
        db.begin_transaction()
    
    try:
        # 1. Find handler for event
        event_type = envelope.get('data', {}).get('type')
        handler_module = _find_handler(event_type)
        
        if handler_module:
            # 2. Project the event (projector handles eventStore)
            db = handler_module.project(db, envelope, time_now_ms)
        else:
            # Handle unknown event type
            if 'unknown' in handler_map:
                unknown_handler = handler_map['unknown']
                db = unknown_handler.project(db, envelope, time_now_ms)
            else:
                # Log error instead of using blocked list
                import logging
                logging.error(f"No handler for event type: {event_type}")
                if auto_transaction:
                    db.rollback()
                return db
        
        # 3. Commit if managing transaction
        if auto_transaction:
            db.commit()
        
    except Exception as e:
        # 4. Rollback on any error
        if auto_transaction:
            db.rollback()
        
        # Log error ephemerally
        import logging
        logging.error(f"Failed to process event: {str(e)}")
        
        raise  # Re-raise for caller to handle
    
    return db

# For batch processing during tick
def handle_batch(db, envelopes, time_now_ms):
    """Process multiple events, each in its own transaction"""
    successful = 0
    failed = 0
    
    for envelope in envelopes:
        try:
            db = handle(db, envelope, time_now_ms, auto_transaction=True)
            successful += 1
        except Exception as e:
            failed += 1
            # Individual failure doesn't stop batch processing
            import logging
            logging.warning(f"Failed to process event in batch: {e}")
    
    return db, successful, failed
```

### Phase 3: Command Processing with Transactions

```python
# core/command.py modifications
INFRASTRUCTURE_PATHS = ['state.outgoing', 'incoming']

def is_infrastructure_update(key, value, db):
    """Validate if an update is to infrastructure state"""
    # Direct infrastructure paths
    if key in ['incoming']:
        return True
    
    # Check state updates
    if key == 'state' and isinstance(value, dict):
        # Only allow updates to infrastructure within state
        current_state = db.get('state', {})
        for state_key, state_value in value.items():
            if state_key == 'outgoing':
                continue  # Infrastructure
            elif state_key in current_state:
                # Check if trying to modify domain state
                if current_state[state_key] != state_value:
                    return False  # Domain state modification attempted
        return True
    
    # All other paths are domain state
    return False

def run_command(handler_name, command_name, input_data, db=None, time_now_ms=None):
    """Run command with transaction support and validation"""
    if db is None:
        db = {}
    
    # Find and load command module
    handler_path = get_handler_path(handler_name)
    command_path = os.path.join(handler_path, f"{command_name}.py")
    command_module = _load_module(command_path)
    
    return db.with_retry(lambda: _run_command_with_tx(
        handler_name, command_name, command_module, input_data, db, time_now_ms
    ))

def _run_command_with_tx(handler_name, command_name, command_module, input_data, db, time_now_ms):
    db.begin_transaction()
    try:
        # Run the command with current signature
        result = command_module.execute(input_data, db)
        
        # Process any domain events within same transaction
        if isinstance(result, dict) and 'newEvents' in result:
            for event in result['newEvents']:
                # Create envelope as in current code
                import uuid
                event_id = str(uuid.uuid4())
                
                metadata = {
                    'selfGenerated': True,
                    'eventId': event_id,
                    'timestamp': time_now_ms or int(time.time() * 1000)
                }
                
                if 'received_by' in event:
                    metadata['received_by'] = event.pop('received_by')
                
                envelope = {
                    'data': event,
                    'metadata': metadata
                }
                
                db = handle(db, envelope, time_now_ms, auto_transaction=False)
        
        # Validate and apply direct updates
        if isinstance(result, dict) and 'db' in result:
            for key, value in result['db'].items():
                if not is_infrastructure_update(key, value, db):
                    raise ValueError(
                        f"Command '{command_name}' attempted to modify domain state '{key}'. "
                        f"Domain state must be modified through events."
                    )
                db[key] = value
        
        db.commit()
        return result
        
    except Exception as e:
        db.rollback()
        raise
```

### Phase 4: Tick Processing with Serialized Batch Transactions

```python
# core/tick.py modifications
from core.job_discovery import discover_jobs
from core.handle import handle_batch
import logging

def tick(protocol_path, db, time_now_ms=None):
    """
    Process tick with batch transaction support.
    Each event is processed in its own transaction, serialized to prevent conflicts.
    """
    
    # Process incoming messages if handler exists
    incoming_handler_path = os.path.join(protocol_path, 'handlers', 'incoming')
    if os.path.exists(incoming_handler_path):
        # Get and clear incoming queue atomically
        def get_and_clear_incoming():
            db.begin_transaction()
            try:
                incoming = db.get('incoming', [])[:]  # Copy list
                if incoming:
                    db['incoming'] = []
                    db.commit()
                    return incoming
                db.rollback()  # Nothing to do
                return []
            except Exception as e:
                db.rollback()
                raise
        
        incoming = db.with_retry(get_and_clear_incoming)
        
        if incoming:
            # Process events sequentially, each in its own transaction
            db, successful, failed = handle_batch(db, incoming, time_now_ms)
            logging.info(f"Tick processed {successful} events, {failed} failed")
    
    # Run all jobs
    jobs = discover_jobs(protocol_path)
    jobs_run = 0
    
    for job_info in jobs:
        def run_job():
            db.begin_transaction()
            try:
                # Load and execute job
                job_module = _load_module(job_info['path'])
                input_data = {'time_now_ms': time_now_ms} if time_now_ms else {}
                result = job_module.execute(input_data, db)
                
                # Process any events from job within same transaction
                if isinstance(result, dict) and 'newEvents' in result:
                    for event in result['newEvents']:
                        # Create envelope
                        envelope = create_envelope_from_event(event, time_now_ms)
                        db = handle(db, envelope, time_now_ms, auto_transaction=False)
                
                # Apply any db updates
                if isinstance(result, dict) and 'db' in result:
                    for key, value in result['db'].items():
                        db[key] = value
                
                db.commit()
                return True
            except Exception as e:
                db.rollback()
                raise
        
        try:
            if db.with_retry(run_job):
                jobs_run += 1
        except Exception as e:
            logging.error(f"Job {job_info['name']} failed after retries: {e}")
            # Continue with next job - one job failure doesn't stop tick
    
    return jobs_run
```

## Implementation Steps

### Phase 1: Core Transaction Support
1. Add transaction methods to PersistentDict (begin/commit/rollback)
2. Implement transaction cache for isolation
3. Ensure backward compatibility (auto-commit when not in transaction)

### Phase 2: Event Processing Transactions
1. Modify handle.py to wrap each event in a transaction
2. Remove 'blocked' list handling - use logging instead
3. Add batch processing support for tick operations
4. Test rollback scenarios with simulated failures

### Phase 3: Command and API Transactions
1. Wrap command execution in transactions
2. Process newEvents within the same transaction
3. Update api.py to use transactions

### Phase 4: Tick and Batch Processing
1. Implement handle_batch for processing multiple events
2. Each event in its own transaction (independent failures)
3. Update tick processor to use batch operations
4. Add performance metrics

## Key Design Decisions

1. **Full serialization**: Use EXCLUSIVE transactions to ensure complete write serialization
2. **One transaction per event**: Each event is processed in its own transaction for isolation
3. **Batch processing for tick**: Process multiple events sequentially, each in its own transaction
4. **Independent failures**: In batch processing, one event failing doesn't affect others
5. **Infrastructure state**: Only `state.outgoing` and `incoming` can be modified directly
6. **Protocol handles dependencies**: Event ordering and dependencies are protocol concerns, not core
7. **Retry with timeout**: Database operations retry with exponential backoff on lock conflicts
8. **Projectors are read-only**: Projectors update state but don't create new events

## Testing Strategy

```python
# Test transaction rollback
def test_projector_failure_rollback():
    db = create_test_db()
    time_now_ms = int(time.time() * 1000)
    
    # Create event that will fail
    failing_envelope = {
        'data': {'type': 'test_fail', 'trigger_error': True},
        'metadata': {'eventId': 'test-123', 'timestamp': time_now_ms}
    }
    
    # Capture state before
    state_before = copy.deepcopy(db.get('state', {}))
    eventstore_before = len(db.get('eventStore', []))
    
    # Process event - should fail and rollback
    with pytest.raises(Exception):
        handle(db, failing_envelope, time_now_ms)
    
    # Verify rollback
    assert db.get('state', {}) == state_before
    assert len(db.get('eventStore', [])) == eventstore_before

# Test batch processing isolation
def test_batch_processing_isolation():
    db = create_test_db()
    time_now_ms = int(time.time() * 1000)
    
    envelopes = [
        {'data': {'type': 'message', 'text': 'hello'}, 'metadata': {'eventId': '1'}},
        {'data': {'type': 'test_fail', 'trigger_error': True}, 'metadata': {'eventId': '2'}},
        {'data': {'type': 'message', 'text': 'world'}, 'metadata': {'eventId': '3'}}
    ]
    
    db, successful, failed = handle_batch(db, envelopes, time_now_ms)
    
    assert successful == 2
    assert failed == 1
    # Verify both valid events were processed
    messages = db.get('state', {}).get('messages', [])
    assert len(messages) == 2
    assert messages[0]['text'] == 'hello'
    assert messages[1]['text'] == 'world'
```

## Migration Strategy

### Phase 1: Add transaction support (backward compatible)
- Deploy PersistentDict with transaction methods
- Existing code continues to work with auto-commit
- No behavior change for current systems

### Phase 2: Update core framework
- Update handle.py to use transactions
- Update command.py with validation
- Deploy with feature flag for gradual rollout

### Phase 3: Update protocols
- Remove any remaining 'blocked' list usage
- Ensure commands only modify infrastructure state
- Run migration scripts to clean up any invalid state

### Phase 4: Enable full serialization
- Enable EXCLUSIVE transaction mode
- Monitor performance impact
- Tune retry and timeout settings based on metrics

## Success Criteria

1. Every event processed atomically with rollback on failure
2. Full write serialization prevents concurrent state corruption
3. Failed events in batch don't affect successful ones
4. Infrastructure updates validated and allowed
5. Zero data loss on timeouts (events can be reprocessed)
6. Performance impact < 10% for typical workloads

## Performance Considerations

- **Serialization tradeoff**: Full serialization ensures consistency but limits concurrency
- **Batch efficiency**: Processing incoming queue as batch amortizes transaction overhead
- **Timeout handling**: 30-second timeout with retry ensures progress even under load
- **Sequential processing**: Events processed one-by-one prevents conflicts

## Summary

This plan addresses all identified issues:

1. **Transaction boundaries**: Every operation is wrapped in a transaction with proper rollback
2. **Write serialization**: EXCLUSIVE transactions prevent concurrent state corruption
3. **Batch processing**: Tick operations process multiple events efficiently
4. **Infrastructure validation**: Commands can only modify allowed infrastructure state
5. **Error handling**: Logging replaces the 'blocked' list for cleaner error tracking
6. **Retry logic**: Timeouts and locks are handled gracefully with exponential backoff
7. **Testing strategy**: Comprehensive tests ensure rollback and isolation work correctly
8. **Migration path**: Gradual rollout minimizes risk

The design maintains clean separation between framework concerns (transactions, serialization) and protocol concerns (event ordering, dependencies).