# Database State Structure Analysis

## Summary
After analyzing all 23 handler.json files across the protocols, I found that **ALL** files consistently use the nested "tables" pattern for database state in tests. This is the standard pattern used throughout the codebase.

## The Standard Pattern

### In `given.db`:
```json
"given": {
  "db": {
    "tables": {
      "table_name": [...],
      "another_table": [...]
    }
  }
}
```

### In `then.tables`:
```json
"then": {
  "tables": {
    "table_name": [...],
    "another_table": [...]
  }
}
```

## Pattern Usage Statistics
- Total handler.json files: 23
- Files using nested "tables" pattern: 23 (100%)
- Files using direct db properties: 0 (0%)

## Examples from Different Protocols

### 1. message_via_tor Protocol
From `protocols/message_via_tor/handlers/identity/identity_handler.json`:

**Projector test:**
```json
"given": {
  "db": {"tables": {}},
  "envelope": {...}
},
"then": {
  "tables": {
    "identities": [
      { "pubkey": "pub123", "privkey": "priv123", "name": "Alice" }
    ],
    "event_store": [
      { "event_type": "identity", "data": {...} }
    ]
  }
}
```

**Command test:**
```json
"given": {
  "db": {
    "tables": {
      "identities": [
        {"pubkey": "pub1", "privkey": "priv1", "name": "Alice"},
        {"pubkey": "pub2", "privkey": "priv2", "name": "Bob"}
      ]
    }
  },
  "params": {}
}
```

### 2. signed_groups Protocol
From `protocols/signed_groups/handlers/user/user_handler.json`:

```json
"given": {
  "db": {
    "tables": {
      "network": {"id": "net_123", "creator_pubkey": "creator_pub"},
      "invites": [
        {"id": "invite_123", "invite_pubkey": "invite_pub_123", "group_id": "group_123"}
      ],
      "groups": [
        {"id": "group_123", "name": "First Group"}
      ]
    }
  },
  "envelope": {...}
},
"then": {
  "tables": {
    "users": [
      { "id": "user_456", "network_id": "net_123", "group_id": "group_123", "pubkey": "new_user_pub", "name": "New User", "invite_id": "invite_123" }
    ]
  }
}
```

### 3. framework_tests Protocol
From `protocols/framework_tests/handlers/message/message_handler.json`:

```json
"given": {
  "db": {
    "tables": {
      "identities": [ {"pubkey": "pubkey1", "privkey": "0"} ],
      "known_senders": [ {"pubkey": "pubkey1"} ]
    }
  },
  "newEvent": {...}
},
"then": {
  "tables": {
    "event_store": [...],
    "messages": [...]
  }
}
```

## Special Cases

### 1. Empty Database State
When representing an empty database:
```json
"given": {"db": {}, "params": {"name": "Bob"}}
```
This is shorthand for:
```json
"given": {"db": {"tables": {}}, "params": {"name": "Bob"}}
```

### 2. Mixed Table Updates
Tests can update multiple tables in a single operation:
```json
"then": {
  "tables": {
    "users": [ {"id": "user_456"} ],
    "messages": [ {"id": "msg_waiting"} ]
  }
}
```

### 3. Complex Relationships
The signed_groups protocol shows complex relationships between tables:
```json
"tables": {
  "blocked_by_id": {
    "user_456": [
      {"event_id": "msg_1", "reason": "Author user_456 not found"}
    ]
  }
}
```

## Consistency Observations

1. **Uniform Structure**: All protocols follow the same nested "tables" pattern
2. **Event Store**: Most tests include an "event_store" table for event sourcing
3. **Table Naming**: Consistent plural naming convention (identities, messages, users, etc.)
4. **Empty States**: Empty databases are represented as `{}` or `{"tables": {}}`
5. **Assertions**: The `then.tables` pattern allows asserting partial state updates

## Conclusion

The database state structure in tests is highly consistent across the entire codebase. The nested "tables" pattern is universally adopted, making it easy to:
- Understand test expectations
- Port tests between protocols
- Maintain consistency when adding new handlers
- Clearly separate database state from other test concerns (params, return values, etc.)

This consistency is a strong architectural decision that enhances code maintainability and readability.