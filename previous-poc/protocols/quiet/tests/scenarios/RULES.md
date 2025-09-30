# Scenario Test Rules

## Core Testing Principles

### 0. No help from conftest.py

We should be using raw, production API and pipeline in a setup that closely mirrors what demo.py uses.

### 1. API-Only Access Rule

All scenario tests MUST use only API calls to interact with the system. Direct database access is PROHIBITED.

✅ **DO**: Use commands through the pipeline runner
```python
pipeline.run(
    protocol_dir='protocols/quiet',
    db=db,
    commands=[{
        'name': 'create_message',
        'params': {...}
    }]
)
```

❌ **DON'T**: Access the database directly
```python
# NEVER do this in scenario tests:
cursor = db.cursor()
cursor.execute("SELECT * FROM messages")
```

### 2. Single Database Architecture

All tests MUST use a single shared database with multiple identities, not separate databases per peer.

✅ **DO**: Create multiple identities in one database
```python
# Single database, multiple identities
alice_id = create_identity(db, "Alice")
bob_id = create_identity(db, "Bob")
```

❌ **DON'T**: Create separate databases
```python
# NEVER do this:
alice_db = create_database()
bob_db = create_database()
```

### 3. Multi-Identity Client Model

The system models multiple peers as multiple identities within a single client, communicating over loopback.

- Each identity is independent
- No "self" concept - API calls must specify identity_id
- Identities communicate through the envelope pipeline
- Transit encryption happens even between local identities

### 4. Identity Isolation

Each API call MUST specify which identity is making the request.

✅ **DO**: Always specify identity_id
```python
pipeline.run(
    protocol_dir='protocols/quiet',
    db=db,
    identity_id=alice_id,  # Specify which identity
    commands=[{
        'name': 'send_message',
        'params': {...}
    }]
)
```

### 5. Event Flow Testing

Tests should verify the complete event flow through the pipeline:

1. Command creates envelope with dependencies
2. Handlers process envelope (crypto, validation, storage)
3. Responders respond to events (e.g., sync responses)
4. Jobs run periodically (e.g., sync requests)

### 6. No Direct Handler/Job/Responder Access

Tests should not directly call handlers, jobs, or responders. These are triggered through the pipeline.

❌ **DON'T**: Call handlers directly
```python
# NEVER do this:
handler = ValidateHandler()
handler.process(envelope, db)
```

✅ **DO**: Let the pipeline trigger handlers
```python
# Commands trigger the full pipeline
pipeline.run(commands=[...])
```

### 7. User Events and Network Membership

- User events express invite relationships
- create_identity does NOT create user events (to avoid duplicates)
- join_as_user creates the user event
- Networks track membership through the users table

### 8. Command Naming Conventions

Use the correct command names as defined in the protocol:
- `join_as_user` (not `join_network`)
- `create_identity` (creates identity only)
- `create_network` (creates network only)

### 9. Test Organization

- Use base classes (e.g., `BaseSingleDBTest`) for common setup
- Group related tests in descriptive test files
- Name tests clearly to indicate what scenario they test

### 10. Async Event Processing

Remember that some events are processed asynchronously:
- Jobs run on schedules
- Responders respond to incoming events
- Use appropriate waits or polling when testing async behavior

## Why These Rules Exist

1. **Real-world simulation**: Tests simulate how real clients interact
2. **API validation**: Ensures the command/query API is complete
3. **Decoupling**: Tests remain valid despite internal changes
4. **Security**: Validates that operations are properly exposed
5. **Correctness**: Ensures multi-identity architecture works properly