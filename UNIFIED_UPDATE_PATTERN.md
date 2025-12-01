# Unified Update Pattern Design

## Overview

The unified update pattern is a framework for implementing Last-Writer-Wins (LWW) convergence for events that represent updates/edits to shared state. It uses **Lamport clocks** for deterministic ordering and **SQL window functions** for efficient winner selection.

## Design Principles

### 1. Lamport Clocks (Global Counter)
- **Purpose**: Provide globally comparable timestamps across all peers
- **Implementation**: Per-peer counter that increments with each event created locally
- **Storage**: `peer_gc_state` table tracks the highest `global_count` seen from each peer
- **Update on Sync**: When receiving events from other peers, update tracking to prevent reuse after restart

### 2. Last-Writer-Wins (LWW) via Window Functions
- **Mechanism**: SQL window function ranks events by `global_count DESC, id_field DESC`
- **Deterministic Tiebreaker**: Lexicographic ordering of event ID breaks ties
- **Partitioning**: Events grouped by partition key (e.g., `message_id` for updates)
- **Winner Selection**: Only events with `rn=1` (rank 1) are projected as current state

### 3. Event Type Registration
All update events must register themselves in `events/network/recorded.py`:
```python
elif event_type == 'message_update':
    from events.content import message_update
    projected_id = message_update.project(ref_id, recorded_by, recorded_at, db)
```

## Implementation Pattern

### Core Functions Required

Every update event type must implement:

1. **`create()`** - Create event with Lamport clock
   ```python
   global_count = updates.get_next_global_count(peer_id, db)
   event_data = {
       'type': 'message_update',
       ...
       'global_count': global_count,
       ...
   }
   ```

2. **`project()`** - Store event in projection table
   - Called by dispatcher
   - Stores raw event data
   - Does NOT select winners (done at query time)

3. **`get()`** - Query winning version at read time
   ```python
   result = updates.get_winners(
       'message_updates',
       'message_id',
       {'message_id': [msg_id], 'recorded_by': recorded_by},
       db,
       id_field='update_id'
   )
   ```

### Database Schema

Each update type needs:
- **Subjective table** (scoped by `recorded_by`):
  - Primary key: `(event_id, recorded_by)` or `(update_id, recorded_by)`
  - Required fields: `global_count`, partition key, content
  - Indexes on partition key and LWW ordering

Example (message_updates):
```sql
CREATE TABLE message_updates (
    update_id TEXT NOT NULL,
    message_id TEXT NOT NULL,  -- partition key
    global_count INTEGER NOT NULL,
    new_content TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (update_id, recorded_by)
);

-- LWW index: partition by message_id, order by global_count DESC, update_id DESC
CREATE INDEX idx_message_updates_winner
ON message_updates(message_id, recorded_by, global_count DESC, update_id DESC);
```

### Database Scoping

Register in `db.py`:
```python
SUBJECTIVE_TABLES = {
    'message_updates',           # Peer-scoped
    'message_reactions',         # Peer-scoped
    ...
}

DEVICE_TABLES = {
    'peer_gc_state',            # Device-wide Lamport clock tracking
    ...
}
```

## Multi-Update Events Example

### Message Reactions (message_reaction.py)
- **Partition by**: `(message_id, reactor_id, emoji)`
- **Winner**: Latest reaction (highest global_count)
- **Deletion**: Via separate `message_reaction_deletion` blocking events
- **Use case**: Multiple people can react to same message with same emoji; LWW picks one winning reaction

### Message Updates (message_update.py)
- **Partition by**: `message_id`
- **Winner**: Latest edit (highest global_count)
- **Authorization**: Only original author can edit
- **Use case**: Message author can edit their message; if edit conflicts, highest global_count wins

## Convergence Guarantee

1. **Deterministic**: All peers apply same tiebreaker (event ID DESC) when global_count ties
2. **Idempotent**: Projecting same event twice yields same result
3. **Causally ordered**: Sync preserves happens-before via event IDs and timestamps
4. **Eventually consistent**: All peers converge to same LWW winner after sync

## Performance Characteristics

### Lamport Clock Overhead
- Single query + insert per create: O(1)
- One extra row in `peer_gc_state` per peer

### Window Function Overhead
- Window function evaluated at query time (lazy)
- Efficient with indexes: O(log N) to find highest global_count
- No extra storage beyond the index

### Comparison with Alternatives
- **Vector clocks**: More data, harder to compare
- **Timestamps alone**: Non-deterministic on clock skew
- **Bloom filters**: Extra metadata bandwidth, false positive waste
- **Lamport + window functions**: Minimal overhead, deterministic, efficient

## Documentation & Testing

### Tests Required
- Single-peer: author edits, multiple edits to same message
- Multi-peer: concurrent edits, convergence verification
- Sync: edits propagate correctly, tiebreaker applies consistently
- History: old events don't overwrite new ones

### Documented Examples
- `tests/scenario_tests/test_message_update.py` - 6 comprehensive tests
- `tests/scenario_tests/test_message_reactions.py` - 5 comprehensive tests
- All tests verify: creation, sync, convergence, authorization

## Current Implementations

### ✅ Message Reactions (message_reaction.py)
- Status: Production ready
- Tests: 5/5 passing
- CLI support: ✅ add-reaction, remove-reaction commands

### ✅ Message Updates (message_update.py)
- Status: Production ready (on feature/message-update branch)
- Tests: 6/6 passing
- CLI support: Not yet implemented

## Future Update Types

Any future update event can follow this pattern:
- Username updates (already using pattern)
- Network name updates (already using pattern)
- Channel topic updates (can use pattern)
- Message pin status updates (can use pattern)

All will automatically get:
- Deterministic convergence
- Efficient LWW resolution
- Multi-peer sync support
- Conflict-free semantics

## Framework Code

All update types rely on shared utilities in `events/_shared/updates.py`:
- `get_next_global_count()` - Get next Lamport clock value
- `update_highest_gc_seen()` - Track seen clocks during sync
- `get_winners()` - Query LWW winners via window functions

This framework ensures consistency across all update types.
