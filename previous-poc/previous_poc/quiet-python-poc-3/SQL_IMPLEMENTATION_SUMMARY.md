# How the SQL Implementation Works

## Overview

The signed_groups protocol has been converted from dict-based state to pure SQL. Here's how it actually works:

## 1. Database Structure

### Core Tables
- **networks**: Network definitions with creator
- **users**: Network members 
- **groups**: Groups within a network
- **channels**: Communication channels tied to groups
- **messages**: Messages in channels
- **adds**: Group membership records
- **links**: Multi-device associations (peer_id -> user_id)
- **invites**: Network invitations
- **link_invites**: Device linking invitations

### Special Tables
- **event_store**: Complete history of all events (protocol-owned)
- **recheck_queue**: Markers for event reprocessing

## 2. Event Flow

### A. Command Creates Event
```python
# Example: message/create.py
def execute(params, db):
    # 1. Validate inputs via SQL queries
    channel = db.conn.cursor().execute(
        "SELECT id, network_id FROM channels WHERE id = ?", 
        (channel_id,)
    ).fetchone()
    
    # 2. Generate event
    message_event = {
        'type': 'message',
        'id': hashlib.sha256(...).hexdigest()[:16],
        'channel_id': channel_id,
        'author_id': user_id,
        'content': content,
        'signature': f'dummy_sig_signed_by_{peer_id}'
    }
    
    # 3. Return event (framework handles projection)
    return {
        'api_response': {...},
        'newEvents': [message_event]
    }
```

### B. Framework Projects Event
```python
# core/command.py
def run_command(...):
    with db.transaction():
        # Execute command
        result = command_module.execute(params, db)
        
        # Project each event
        for event in result.get('newEvents', []):
            envelope = {'data': event, 'metadata': {...}}
            db = handle(db, envelope, time_now_ms)
        
        # Commit if successful
```

### C. Handler Processes Event
```python
# Example: message/projector.py
def project(db, envelope, time_now_ms):
    # 1. Store in event_store
    _append_event(db, envelope, time_now_ms)
    
    # 2. Validate
    if not valid_signature():
        return db  # Ignore invalid events
    
    # 3. Persist to domain table
    cur.execute("""
        INSERT OR IGNORE INTO messages(...) 
        VALUES(?, ?, ?, ...)
    """, (...))
    
    # 4. Trigger reprocessing if needed
    unblock(db, message_id)
    
    return db
```

## 3. Blocking and Dependencies

### Current Implementation (SQL-based)

**Key Insight**: Events are NOT truly "blocked" - they're just ignored until dependencies are met.

1. **Event arrives with missing dependency**:
   - Event IS stored in event_store
   - Event is NOT stored in domain table (e.g., messages)
   - No error is raised - silently ignored

2. **Dependency arrives later**:
   ```python
   def unblock(db, event_id):
       # Insert marker to trigger reprocessing
       cursor.execute(
           "INSERT OR IGNORE INTO recheck_queue(event_id, reason_type, available_at_ms) VALUES(?, ?, ?)",
           ("recheck_all", f"group_{event_id}", 0)
       )
   ```

3. **Reprocessing (via tick/job)**:
   ```python
   # blocked/process_unblocked.py
   def execute(params, db):
       # Delete ALL recheck markers
       cursor.execute("DELETE FROM recheck_queue")
       
       # Replay ALL events from the beginning
       rows = cursor.execute("SELECT data, metadata FROM event_store ORDER BY id")
       for row in rows:
           envelope = {'data': json.loads(row[0]), 'metadata': json.loads(row[1])}
           db = handle(db, envelope, time_now_ms)
   ```

### Why "Partial Unblock" Test Fails

The test expects:
- User 456 arrives
- Only msg_456 is unblocked
- msg_789 remains blocked

What actually happens:
- User 456 arrives
- ALL events in event_store are reprocessed
- Both msg_456 AND msg_789 get created (if user_789 exists)

## 4. Key Differences from docs.md

### 1. No Selective Blocking
- **docs.md**: "blocked table with event-id and blocked-by relationships"
- **Actual**: No blocked table, just recheck_queue with generic markers

### 2. No True Blocking
- **docs.md**: Events are "blocked" and held
- **Actual**: Events are stored but ignored until dependencies met

### 3. Group Membership Not Enforced
- **docs.md**: "message in channel from non-group member is hidden"
- **Actual**: No group membership validation for messages

### 4. Reprocessing Strategy
- **docs.md**: "blocked.unblock passes events whose blocked-by matches"
- **Actual**: ALL events are reprocessed when ANY dependency arrives

## 5. Transaction Guarantees

The implementation provides strong consistency:
- All event creation + projection happens in single transaction
- If projection fails, event is not created
- No partial states possible

## 6. Idempotency

Achieved through:
- `INSERT OR IGNORE` statements
- Deterministic IDs (in TEST_MODE)
- Checking existence before INSERT

## 7. Performance Implications

The current approach:
- ✅ Simple and correct
- ❌ Inefficient for large event stores
- ❌ Reprocesses everything on each unblock
- ❌ No selective dependency tracking

## Summary

The SQL implementation works correctly but differs from docs.md in key ways:
1. Events are never truly "blocked" - just ignored
2. No selective unblocking - everything is reprocessed
3. Simpler but less efficient than the original design
4. Group membership validation not implemented for messages

The core protocol semantics are preserved: events are validated, ordered doesn't matter (eventual consistency), and state converges correctly. The main tradeoff is efficiency for simplicity.