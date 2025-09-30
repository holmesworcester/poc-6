# SQL Implementation Flow Example

Let me trace through a concrete example of how the SQL implementation works.

## Scenario: Creating a Message

### 1. Command Execution (`message/create.py`)

```python
# User calls API to create message
params = {
    "channel_id": "channel_123",
    "user_id": "user_123", 
    "peer_id": "user_123",
    "content": "Hello, world!"
}
```

The command:
1. Validates channel exists via SQL query:
   ```sql
   SELECT id, network_id FROM channels WHERE id = ?
   ```

2. Validates user exists in channel's network:
   ```sql
   SELECT id, pubkey, network_id FROM users 
   WHERE id = ? AND network_id = ?
   ```

3. Creates a message event:
   ```python
   message_event = {
       'type': 'message',
       'id': 'abc123...',  # SHA256 hash
       'channel_id': 'channel_123',
       'author_id': 'user_123',
       'peer_id': 'user_123',
       'user_id': 'user_123', 
       'content': 'Hello, world!',
       'signature': 'dummy_sig_signed_by_user_123'
   }
   ```

### 2. Event Processing (`core/command.py`)

The `run_command` function:
1. Begins a database transaction
2. Executes the command (returns event)
3. Projects the event through handlers:
   ```python
   envelope = {'data': event, 'metadata': {...}}
   db = handle(db, envelope, time_now_ms)
   ```
4. Commits transaction if successful

### 3. Event Handling (`core/handle.py`)

The `handle` function:
1. Determines handler from event type: `message`
2. Calls message handler's projector

### 4. Message Projection (`message/projector.py`)

```python
def project(db, envelope, time_now_ms):
    data = envelope.get('data', {})
    
    # 1. Store in event_store (protocol-specific)
    _append_event(db, envelope, time_now_ms)
    
    # 2. Validate signature
    if signature.startswith("dummy_sig_signed_by_"):
        signer = signature.replace("dummy_sig_signed_by_", "")
        if signer != str(peer_id):
            return db  # Invalid signature
    
    # 3. Check peer/user relationship
    if peer_id != user_id:
        # Must be linked device
        row = cur.execute(
            "SELECT 1 FROM links WHERE peer_id = ? AND user_id = ?",
            (peer_id, user_id)
        ).fetchone()
        if not row:
            return db  # Not authorized
    
    # 4. Insert into messages table
    cur.execute("""
        INSERT INTO messages(id, channel_id, author_id, 
                           peer_id, user_id, content, created_at_ms)
        VALUES(?, ?, ?, ?, ?, ?, ?)
    """, (message_id, channel_id, author_id, 
          peer_id, user_id, content, time_now_ms))
    
    return db
```

### 5. SQL Schema

The data is stored in these tables:

```sql
-- Event history
CREATE TABLE event_store (
    id INTEGER PRIMARY KEY,
    event_id TEXT,
    event_type TEXT,
    data TEXT,        -- JSON
    metadata TEXT,    -- JSON
    created_at_ms INTEGER
);

-- Message data
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    peer_id TEXT,
    user_id TEXT,
    content TEXT,
    created_at_ms INTEGER NOT NULL
);
```

## Scenario: Event Dependencies and Blocking

### What happens when events arrive out of order?

1. **Message arrives before user exists:**
   - Message projector runs
   - No validation of user existence (current implementation)
   - Message is created anyway
   - This differs from docs.md which says it should be blocked

2. **Add event arrives before group exists:**
   - Add projector validates group exists:
     ```python
     g = cur.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,)).fetchone()
     if not g:
         return db  # Group doesn't exist, ignore event
     ```
   - Event is stored in event_store but not in adds table
   - When group arrives later:
     - Group projector calls `unblock(db, group_id)`
     - This inserts "recheck_all" into recheck_queue
     - Next tick runs `blocked/process_unblocked`
     - ALL events are reprocessed from event_store
     - Add event now succeeds

### The Reprocessing Flow

```python
# blocked/process_unblocked.py
def execute(params, db):
    # 1. Get all recheck markers
    rows = cursor.execute(
        "SELECT event_id FROM recheck_queue ORDER BY available_at_ms LIMIT 1000"
    ).fetchall()
    
    # 2. Clear the queue (process everything)
    cursor.execute("DELETE FROM recheck_queue")
    
    # 3. Replay ALL events from beginning
    rows = cursor.execute("SELECT data, metadata FROM event_store ORDER BY id").fetchall()
    for row in rows:
        data = json.loads(row[0])
        metadata = json.loads(row[1])
        db = handle(db, {'data': data, 'metadata': metadata}, time_now_ms)
```

## Key Points

1. **Atomicity**: All operations happen in transactions managed by the framework
2. **Idempotency**: Projectors use INSERT OR IGNORE to handle duplicate events
3. **No Selective Blocking**: When any dependency is satisfied, ALL events are reprocessed
4. **Event Store**: Complete history maintained for reprocessing
5. **SQL-Only**: No dict state, pure SQL queries and inserts

## What's Missing vs docs.md?

1. **Selective blocking**: Can't unblock only specific events
2. **Group membership checks for messages**: Messages from non-members aren't filtered
3. **True blocked table**: Using recheck_queue instead of blocked_by relationships

The implementation is simpler but less efficient - it trades selective unblocking for a simpler "reprocess everything" approach.