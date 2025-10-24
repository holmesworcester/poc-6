# NOTES:

- Bootstrap flow: Invite link contains signed invite event blob. Invitee stores it as an event, creates user event referencing it. User projection extracts metadata from invite, creates stub group/channel rows. This enables immediate messaging before sync completes.


Observations: 

- simplifying sync.py seems important 
- receive has a ton of bootstrapping-related garbage in it
- test_sync_perf_10k seems pretty good, though it's not clear if/why db.commmit() calls are necessary
- test_sync_bloom seems more suspect
- there's some question about whether id's should be blob or text but right now they're text

# SIMPLIFYING SYNC

1. route (unsafe / device-wide) - look up the peers for transit id, call handle
1. if we routed first, we wouldn't need to worry about responding to our own requests
1. remove send_bootstrap_response
1. combine project and project_sync_event
1. we could have some generic verification that all events use, and sync would benefit from this
1. do we still need transit key dict now that we have separate wrap_event and wrap_transit

# TODO:

- it would be great if bootstrap status depended on some state we could check easily without saving to a table.
- bootstrap is very not dag-like because we have events getting created out of nothing. what's the best way to do this? 
- some events are over the max size now. get a handle on that.
- consider a wire format that is regular with fixed field lengths for each event type
- check how bootstrap works. it should not be per-peer-relationship
  should be per network (per user.join attempt) 
- transit_prekey_shared.py is another place where we could be calling it with more useful parameters to save a lookup and remove brittleness, but I'm not sure. maybe we should just have a store.get_json)id, db)
- why is the logging totally random whether we're logging 10 or 20 chars of id's?
- get_sync_state and update_sync_state could be using safedb because it's for a peer pair
- marked_window_synced seems like it could be simplified 
- in unwrap_transit it seems like we're calling it per-recorded-by instead of *learning* the recorded_by from the transit key, but at the same time we do need to try for each peer since invite links might use the same key even with different peers. this merits more thought!
- consider an ephemeral event store for sync and transit keys that's not reprojected.
- look at syncing loop in tests and remove the necessity for this by adding this to bootstrap and checking bootstrap 
- bootstrap_status.sql should be in transit not identity? what events correspond to it? 
- break out the logic for detecting bootstrap from sync.receive into its own function.  
- stop manually sending bootstrap events in the test. this should happen automatically on sync.
- add dropped packets to network sim
- look at how key_lookup is used. it seems like transit and group key lookup should be separated and built into unwrap. we should probably have separate unwraps for transit (device-wide) and group (safe)
- it would be useful to have local graph style tests where we can force project some set of events as valid, and then test if a subsequent event is valid. like a "graph_neighborhood" test or something. this would make it easier to break up the scenario tests into smaller pieces and focus on key invariants. 
- make a sync receive test that includes lots of invalid events of different kinds and ensures we get the right behavior
- have a test for a broken invite link to make sure joining fails due to missing proof
- drop `address` event from the bootstrap flow and instead let the recipient create `address` events for peers.
- make group-member depend on the group-member of the person adding, so that missing deps will block and we can rely on our projections to be queryable synchronously. (and think about this.) --and generally review group-member and group events.
- reduce the number of non-subjective tables and events (break out transit keys/prekeys from group keys e.g.)
- try to use unsafedb as rarely as possible 
- have a look at invite key and prekey creation to make sure it makes sense
- is there anywhere i should *not* be using INSERT OR IGNORE?
- user verification looks squirrelly in user.project ( # Verify signature in particular)
- read through the convergence test and make sure it is simple and clear
- get check for deps out of crypto unwrap.
- ongoing group encryption, adding members
- deletion
- removal
- files

# Testing

Run all tests:
```bash
./run_tests.sh
```

Run specific test file:
```bash
./run_tests.sh tests/test_safe_scoping.py
```

Run tests matching a pattern:
```bash
./run_tests.sh -k test_peer_isolation
```

**For LLMs:** Always use `./run_tests.sh` to run tests for consistency.

# Debugging and Logging

## Structured Logging for LLM Debugging

This codebase uses **structured logging** designed to be easily parsed and understood by LLM assistants during debugging. The approach makes execution flow crystal clear and eliminates confusion when tracing issues.

### Logging Pattern

**Format:** `[TAG] key1=value1 key2=value2 ...`

**Key principles:**
1. **Tagged sections** - Uppercase tags in brackets group related operations (e.g., `[BOOTSTRAP_SEND]`, `[UNWRAP_START]`)
2. **Key-value pairs** - Structured data instead of prose (e.g., `hint=abc123... peers=['xyz...']`)
3. **Short IDs** - First 10-20 characters of hashes for readability while maintaining uniqueness
4. **Result fields** - Explicit outcomes (e.g., `result=found_3_peers`, `result=blob_not_found`)
5. **Correlation IDs** - Track entities across function calls (e.g., `hint`, `peer_id`, `key_id`)

### Example Log Sequence

```
[BOOTSTRAP_SEND] from_peer=wMmT14UfFO... to_inviter=n51phxWWy0... invite_transit_key_id=fAjNFEUK... sending_4_events
[BOOTSTRAP_SEND] event=peer_shared blob_size=296B hint=fAjNFEUK...
[BOOTSTRAP_SEND] event=user blob_size=494B hint=fAjNFEUK...
[SYNC_RECEIVE] batch_size=20 drained=5_blobs t_ms=4100
[UNWRAP_START] blob_size=296B hint=fAjNFEUK...
[UNWRAP_TRANSIT_KEY] hint=fAjNFEUK... result=found_1_peers peers=['hsl+wW22L4...']
[TRANSIT_KEY_PROJECT] key_id=fAjNFEUK... recorded_by=hsl+wW22L4... result=inserting
```

### Benefits for LLM Debugging

1. **Easy correlation** - Match events across function boundaries using hint/peer_id
2. **Clear flow** - Tags show operation sequence (SEND → RECEIVE → UNWRAP → PROJECT)
3. **Fast diagnosis** - Result fields immediately show success/failure
4. **No ambiguity** - Structured format eliminates natural language confusion
5. **Grep-friendly** - Easy to filter logs by tag: `grep '\[UNWRAP'`

### When to Use Structured Logs

Use structured WARNING-level logs for:
- **Entry/exit points** of major operations (sync, unwrap, project)
- **Decision outcomes** (found peers, missing deps, blocked events)
- **State transitions** (event becomes valid, unblocking)
- **Cross-boundary communication** (bootstrap send/receive, sync request/response)

Use regular INFO/DEBUG for:
- Internal function logic details
- Verbose iteration loops
- Temporary debugging during development

# Quiet Protocol Proof of Concept #6

This is a proof-of-concept protocol for eventually consistent state syncing.

The goal with this attempt is to avoid the framework/protocol distinction of previous attempts and take advantage of the simplicity of building what I need now and nothing else.

I want to keep lines of code to a minimum.

Rules:

- Everything is stored in a single SQLite file
- All application data, local and shared, for all networks, is expressed in an event stource
- We use the "projection" event sourcing pattern where events are "projected" into whatever standard relational database best fits application needs
- A single client can have multiple "peers" (identities) participating in different networks or the same networks.
- To test multiple clients, we test a single client in multiple networks
- For any tests involving a db, "scenario tests" are preferred, and these must *ONLY* build, mock, and test state via realistic "API" usage ("API" in quotes because tests can call functions in queries.py and commands.py directly with their provided params)

Helpful lessons from last time:

- db is not in-memory, it is a real sqlite db
- it is useful to for each event type to have a 
- Goal: functions in each are simple enough that 

# Design

## Event Structure and Projection Pattern

### Standardized Event Fields

All events follow a consistent structure with these standard fields:
- `type`: Event type identifier (e.g., 'message', 'group')
- `created_by`: Peer ID who created the event
- `created_at`: Timestamp (ms) when the event was created

Field names are standardized across all event types. Database columns may differ from event field names, but this is explicit in the projection code.

### Recorded Pattern

The `recorded` event type wraps references to other events and tracks:
- **WHO** recorded the event (via `recorded_by`)
- **WHEN** they recorded it (via `recorded_at`, derived from recorded's `created_at`)
- **WHAT** event they recorded (`ref_id` = referenced event ID)

This enables multi-peer support within a single client, as each peer has their own view of which events they've recorded and when.

#### `recorded_by` Semantics

The `recorded_by` field identifies which peer recorded an event, but its value depends on how the event arrived:

**For locally created events:**
- `recorded_by` = `creator_id` (the peer_id of the peer who created the event)
- The creator immediately records their own event
- Example: When peer_id A creates a message, `recorded_by` = A

**For incoming transit blobs (during sync):**
- `recorded_by` = peer ID corresponding to the transit_key used for wrapping
- The transit_key is determined by the receiving peer's original sync request
- This is looked up via `key.get_peer_ids_for_key(hint, db)`
- Example: When peer B receives a blob wrapped with B's transit_key, `recorded_by` = B
- **Important:** `recorded_by` ≠ `creator_id` for incoming events

**Multiple recorded events per blob (edge case):**
- When multiple local peers have the same key (e.g., two peers in the same network both accepted the same invite), an incoming blob can be decrypted by multiple peers
- In this case, `sync.unwrap_and_store()` creates a **separate recorded event for each peer** who has access to the key
- Each peer gets their own independent view of when they recorded the event
- Example: If peers B and C both joined via the same invite link, they both have the invite's symmetric key. When an invite bootstrap event arrives, both peers get a recorded event.

In the `recorded` event itself, `created_by` equals `recorded_by`, but the source of this value differs based on the creation path.

#### Timing Semantics

All timestamps are in milliseconds since epoch.

All events in the event store have two timestamps:
- **`created_at`** (field in event data): When the event was created by its author
- **`stored_at`** (column in store table): When the event blob was stored in the local database

For different event types:
- **Message events**: `created_at` = author's creation time (may be in the past when received); `stored_at` = when stored locally
- **recorded events**: `created_at` = `stored_at` = when this peer recorded the referenced event (both set to the same `t_ms`)

In projections:
- **`recorded_at`** is extracted from `recorded.created_at` - represents when this peer recorded the referenced event
- **`created_at`** is extracted from the referenced event (e.g., message) - represents the original author's creation time
- The messages table stores both: sort/display by message's `created_at`, filter by peer via `recorded_at`

The `stored_at` values exist in the event store but are not typically projected into application tables. Only the `stored_at` of recorded events is semantically meaningful per-peer (and we use `recorded.created_at` which equals it).

### Projection Flow

1. `recorded.project(recorded_id, db)` is called
2. Extracts `recorded_by` from recorded event's `created_by` field (who recorded this event)
3. Extracts `recorded_at` from recorded event's `created_at` field (when they recorded it)
4. Projects to recorded table: `(recorded_id, event_id, recorded_by, recorded_at)`
5. Loads the referenced event and extracts its `created_at` (original author timestamp)
6. Calls event-specific projector: `message.project(event_id, db, recorded_by, recorded_at)`
7. Event projector inserts into its table with both timestamps: message's `created_at` (author time) and `recorded_at` (peer's recording time)

### Multi-Peer Support

All projected state is scoped per-peer via `recorded_by`:
- Messages table includes `recorded_by` column
- List queries filter by `recorded_by`: `WHERE channel_id = ? AND recorded_by = ?`
- This allows a single client to manage multiple peers in the same or different networks

**List function pattern:**
- All list functions follow the pattern: `list_*(filter_params..., recorded_by, db)`
- Examples:
  - `list_messages(channel_id, recorded_by, db)` - filters by channel and peer
  - `list_all_groups(recorded_by, db)` - filters by peer only (groups aren't scoped to channels)
- The `db` parameter always comes last for consistency across the codebase

### Shareable Events and Sync Ordering

**Problem:** Events must be marked as shareable (for bloom-filtered sync) before we know their exact `created_at` timestamp. This is because some events are encrypted and can't be decrypted until later when keys arrive. Without a consistent `created_at` value, different projection orders produce different final states, breaking convergence.

**Solution:** Two-phase approach with centralized update:
1. **Early marking:** When an event enters the projection pipeline, mark it as shareable immediately (with explicit `created_at` from event data)
   - Only adds shareable_events if `event_data.created_at` is present in the event blob
   - For encrypted events with no plaintext metadata, defers shareable marking to projection phase
   - Ensures blocked/encrypted events can still be synced once decoded
2. **Authoritative update:** After successful projection to the event's table, query that table to get the true `created_at` and update `shareable_events`
   - Single centralized location in `recorded.project()` after validation
   - Uses `_get_authoritative_created_at()` helper with table mapping
   - Updates via: `INSERT OR REPLACE INTO shareable_events (event_id, can_share_peer_id, created_at, recorded_at, window_id) VALUES (...)`
   - This ensures shareable_events always has authoritative `created_at` from projection table

**Critical Invariant:** `shareable_events.created_at` is ALWAYS canonical from the event's projection table - NEVER from `recorded_at`. This is essential for convergence:
- Different peers may record the same event at different times (`recorded_at` differs)
- But all peers must agree on when the event was originally created (`created_at` is the same)
- Using `recorded_at` as a fallback would create different `created_at` values depending on projection order
- The `INSERT OR REPLACE` pattern ensures even if an event is marked shareable early, it gets corrected to the authoritative value during projection

**Design invariant:** All shareable event types store `created_at` in their projection tables (strict enforcement). Users table renamed from `joined_at` → `created_at` for consistency. This guarantees we can always query the authoritative value after projection.

**Example - Message Event Convergence:**
```
Scenario: Peers A and B receive the same message from C in different orders

Without the fix:
- A processes: recorded event at t=1000 → creates shareable_event with created_at=1000 (recorded_at fallback) → BAD
- B processes: recorded event at t=2000 → creates shareable_event with created_at=2000 (recorded_at fallback) → BAD
- Result: Different created_at values, convergence broken!

With the fix:
- Both A and B: message event has explicit created_at=500 (from message.created_at field)
- Both A and B: insert shareable_event with created_at=500 (from event data)
- Both A and B: during projection, INSERT OR REPLACE updates with created_at=500 (from messages table)
- Result: Same created_at=500 on both peers, convergence guaranteed!
```

**Storage Pattern:**
- `shareable_events.created_at` - Canonical event creation time (from projection table)
- `shareable_events.recorded_at` - When this peer recorded the referenced event (from recorded.created_at)
- These are two **independent** timestamps that serve different purposes
- Never use recorded_at as a fallback for created_at

### Cascading Deletion

**Problem:** When an event is deleted, its dependent events must also be removed from `valid_events` to ensure convergence. Otherwise, different event orderings produce different final states:
- If deletion arrives first: event is blocked from projecting
- If event arrives first: event projects and must be removed upon deletion
- Without cascading deletion: different states in different orderings!

**Solution:** Generic dependency tracking with transitive cascading deletion:

**Implementation:**
1. **`event_dependencies` table** - Tracks parent→child relationships (child depends on parent)
   - Schema: `(child_event_id, parent_event_id, recorded_by, dependency_type)`
   - Index on `(parent_event_id, recorded_by)` for efficient cascade lookup

2. **Populate during projection** - Each `*.project()` function records its dependencies
   - Message depends on channel: `INSERT INTO event_dependencies VALUES (message_id, channel_id, recorded_by, 'channel')`
   - Attachment depends on message and file
   - File slice depends on file
   - Generic pattern for future event types

3. **Cascade on deletion** - When an event is deleted, recursively remove all dependents
   - `cascade.cascade_delete_from_valid_events(event_id, recorded_by, safedb)`
   - Depth-first traversal with cycle detection
   - Removes event and all its transitive dependents from `valid_events`

**Why this ensures convergence:**
- **Order A (event first, then deletion):** Event projects → deletion cascades → event removed from valid_events
- **Order B (deletion first, then event):** Deletion marks event as deleted → event blocked from projecting → not in valid_events
- Both orderings produce identical final state!

### Invite Link Design

**Architecture:** Minimal signed invite event as single source of truth for join authorization.

**Invite Event Structure:**
- Signed by inviter (Alice) using peer private key
- Contains: `invite_pubkey`, `invite_key_secret`, `group_id`, `channel_id`, `key_id`
- Stored as canonicalized JSON blob, included in invite link (~882 chars, 56% reduction from previous)

**Invite Link Contents:**
- Invite event blob (signed)
- Inviter's peer_shared blob (NEW: for immediate bootstrap)
- Symmetric keys for bootstrap and network access
- Inviter's network address (IP/port)

**Join Flow (Bob):**
1. Extracts invite blob from URL, stores as event → `invite_id`
2. Projects inviter's peer_shared from invite link → Alice appears in Bob's peers_shared table immediately
3. Creates `user` event: references `invite_id`, wrapped with `invite_key` (not network key)
4. Creates `network_joined` event: marks bootstrap complete with inviter
5. User projection: fetches invite, validates Alice's signature, extracts metadata, creates stub group/channel rows
6. Bob can message immediately (has stubs), sync fills in full details later

**Bootstrap Mode:**
- When Bob calls `sync_all()`, checks `sync_state_ephemeral.bootstrap_complete`
- If 0: sends ALL shareable events to peer (bootstrap mode)
- If 1: sends incremental bloom-filtered sync (normal mode)
- Network creator and joiners create `network_created` and `network_joined` events to mark bootstrap complete

**Dependency Model:**
- User events depend on `invite_id` (automatic blocking/unblocking)
- Third peers: receive user before invite → block → invite arrives → unblock & project
- User projection is deterministic: same invite → same stubs for all peers

**Key Insight:** User event is the membership credential. Projection grants minimal access (stubs). Real group/channel events upgrade stubs to full data via `INSERT OR REPLACE`.

**Future Enhancement (Multi-Peer Bootstrap):**
Note: Invite links currently include a single inviter's peer_shared blob. In the future, this could be extended to include multiple bootstrap peers for high availability (e.g., include backup server's peer_shared blob alongside inviter's). The invite would still be signed by the original inviter (trust anchor), but joiners could immediately sync with multiple peers for redundancy. This requires no protocol changes - just populating multiple entries in the invite link's bootstrap peers array.

### Dependency Resolution: Local vs Network Dependencies

Event dependencies fall into two categories based on **whose perspective** is processing the event:

**Network Dependencies** (always checked):
- References to shared events that should arrive via sync
- Examples: `group_id`, `channel_id`, `created_by` (peer_shared IDs)
- If missing, event is blocked until dependency arrives

**Local Dependencies** (perspective-dependent):
- References to creator's private data (never shared over network)
- Examples: `peer_shared.peer_id` → creator's local peer, `key.peer_id` → owner's local peer
- **When creator processes:** checked normally (should exist locally)
- **When others process:** skipped (foreign local dep, will never arrive)

**Implementation** (`events/recorded.py::is_foreign_local_dep()`):
```python
# Skip peer_id check only if we're NOT the creator
if event_type in {'peer_shared', 'key'} and field == 'peer_id':
    return recorded_by != created_by
```

This enables:
- **More precise validation** - catches missing network deps
- **Works during reprojection** - decision based on event schema + recorded_by (no DB state needed)
- **Simpler special cases** - most event types use normal dependency checking

### Event Encryption and Relay Design

The protocol uses **two-layer wrapping** with **separate key namespaces** to prevent mixing routing and content keys:

**Layer 1: Transit Wrapping (Network Routing)**
- Determines WHO can store and forward an event
- Wrapped with sync request's `transit_key` (symmetric, ephemeral per sync)
- Success = routing permission granted
- Handled in `sync.unwrap_and_store()` via `crypto.unwrap_transit()`
- **Key namespace**: `transit_keys`, `transit_prekeys` ONLY
- **Blocking**: Never blocks - drops unknown blobs (DoS protection)
- **Key lookup**: `key_lookup.get_transit_key_by_id()` - only checks local transit keys we own

**Layer 2: Event Content Wrapping (Application Privacy)**
- Determines WHO can read event content
- Wrapped with group keys (symmetric) or group prekeys (asymmetric)
- May fail even when transit succeeds
- Handled in `recorded.project()` via `crypto.unwrap_event()`
- **Key namespace**: `group_keys`, `group_prekeys` ONLY
- **Blocking**: Always blocks on missing keys (convergence guarantee)
- **Key lookup**: `key_lookup.get_event_key_by_id()` - only checks local group keys we own

**Key Namespace Separation:**
- Transit and event layers use completely separate key tables
- Transit unwrap NEVER checks group keys (prevents accidental content exposure at routing layer)
- Event unwrap NEVER checks transit keys (prevents DoS by forcing event blocking on transit keys)
- Key lookup functions ONLY return keys with private keys (decryption), not public keys from `*_shared` tables
- This architecture ensures routing keys cannot decrypt content, and content keys cannot be used for DoS

**Shareable Event Marking (Centralized in recorded.py)**

Events are marked as shareable based on whether we can decrypt them OR whether they're encrypted at all:
- **Plaintext events**: Marked as shareable immediately
- **Encrypted events we CAN decrypt**: Marked as shareable after successful event-layer unwrap
- **Encrypted events we CANNOT decrypt**: Blocked until keys arrive (never marked shareable while blocked)

This happens in `recorded.project()` BEFORE blocking, so blocked events are still shareable and forwarded during sync.

Benefits:
1. **Relay/forwarding of blocked events** - Peers forward messages they can't project yet (missing semantic dependencies like group_id)
2. **Mesh network routing** - Events propagate even when individual peers can't fully process them
3. **Simpler projectors** - Type-specific code doesn't handle shareable marking

**Local-only events** (peer, transit_key, group_key, transit_prekey, group_prekey, recorded, network_created, network_joined, invite_accepted) are explicitly excluded from shareable marking and never sync to other peers.

**Blocking Behavior for Wrapped Events:**

When a peer creates an event wrapped to another peer's prekey (e.g., `group_key_shared` wrapped to Bob's transit prekey):
- Creator stores the wrapped blob locally
- Creator attempts to project via `recorded.project()` → `crypto.unwrap_event()`
- Event-layer unwrap fails (creator doesn't have recipient's private key)
- Event is BLOCKED with missing key dependency
- Event is still marked as SHAREABLE (happens before blocking)
- Recipient receives event via sync, can decrypt and project successfully

This ensures proper convergence - creators can share wrapped events they cannot read themselves.

### Testing Requirements

- **No `time.time()` calls** - all timestamps must be explicit parameters for deterministic testing
- `recorded_at` comes from `recorded.created_at` (which equals the `t_ms` passed to `store.event()`)
- Tests can control time progression by passing explicit `t_ms` values to all event creation and storage functions

### Transaction Ownership Policy

Database transactions follow a clear ownership pattern to ensure atomicity and prevent partial commits:

**Transaction Owners (commit at the end):**
- **Sync entry points**: `sync.receive()` and `sync.send_requests()` commit after completing batch operations
- **Test code**: Tests explicitly call `db.commit()` after operations they want to persist
- **Future API layer**: Will commit after API write operations (not yet implemented)

**Helper Functions (never commit):**
- Event creation functions (`message.create()`, `group.create()`, etc.) - no commits
- Projection functions (`*.project()`) - no commits
- Queue operations (`queues.blocked.*`) - no commits
- Storage functions (`store.event()`, `store.blob()`) - no commits

**Rationale:**
- **Atomicity**: All operations in a workflow succeed or fail together
- **Composability**: Helper functions can be called from different contexts (sync, API, tests) without premature commits
- **Clear boundaries**: Transaction scope is obvious at entry points

**Example flows:**
```python
# Sync flow - sync owns transaction
sync.receive()
  ├─ unwrap_and_store() → recorded.project_ids() → various projections
  └─ db.commit()  # Single atomic commit

# Test flow - test owns transaction
message.create() → store.event() → projection  # No commits
message.create() → store.event() → projection  # No commits
db.commit()  # Test commits when ready

# Future API flow - API layer owns transaction
api_handler()
  ├─ message.create() → projection  # No commits
  └─ db.commit()  # API commits after operation
```

## Scenario Tests

Scenario tests validate end-to-end functionality by simulating realistic API usage patterns. These tests are critical for ensuring the system works correctly from a user/frontend perspective.

### Principles

1. **API-Only Testing**: Scenario tests must ONLY interact with the system through command and query functions (e.g., `peer_secret.create()`, `message.list_messages()`, etc.). Treat these as if they were API endpoints being called by a frontend.

2. **No Direct Database Inspection**: Test assertions must NEVER use direct database queries (e.g., `SELECT * FROM messages`). All verification must be done via:
   - Returned data from command functions (e.g., the `{id, latest}` dict from `message.create_message()`)
   - Query function results (e.g., `message.list_messages()`, `channel.list_channels()`)

3. **Realistic Flows**: Tests should follow realistic user workflows, creating all necessary prerequisites (identity, groups, channels) before performing the main test action.

### Example

See `test_one_player_messaging.py` for a complete scenario test where Alice:
1. Creates her identity (`peer_secret`, `peer`)
2. Creates encryption keys (`key_secret`)
3. Creates a group and channel
4. Sends messages to herself
5. Verifies messages are visible via query functions

All verification is done through returned IDs and query results - no direct DB access.

## Event-Sourcing Requirement

**Core Principle: All non-ephemeral state changes MUST come from events.**

Any command or operation that creates projection table state must go through the event store:

1. **Create an event** - Store the action as a JSON event blob in the `store` table
2. **Wrap with recorded** - Create a `recorded` event to track who recorded it and when
3. **Project to tables** - The event's projection function creates/updates projection table rows

### Ephemeral vs Projection State

**Projection State** (event-sourced):
- Created by projecting events from the store
- Must be rebuildable from events alone
- Examples: `peers`, `groups`, `channels`, `messages`, `keys` (from key events), `users`

**Ephemeral State** (not event-sourced):
- Temporary scheduling/optimization data
- Tables suffixed with `_ephemeral` are excluded from reprojection tests
- Examples: `sync_state_ephemeral`, `blocked_events_ephemeral`, `blocked_event_deps_ephemeral`

### Example: Invite Processing

When Bob joins via invite link, he stores the invite event and invite secrets must be restored during projection.

**Wrong approach** (direct table insertion):
```python
# BAD: Bypasses event store
db.execute("INSERT INTO keys (key_id, key, ...) VALUES (?, ?, ...)", ...)
```

**Correct approach** (event-sourced):
```python
# Invite event contains invite_key_secret
invite_blob = extract_from_url(invite_link)
invite_id = store.event(invite_blob, peer_id, t_ms, db)

# invite.project() restores invite_key_secret to keys table
# This happens during reprojection, making state rebuildable
```

During reprojection, replaying all `recorded` events recreates projection state. Without event-sourcing (direct inserts), state would be lost.

## Identity Isolation and Access Control

Since a single client can have multiple local peers (identities), all data access functions must prevent one identity from accessing another identity's private data.

### Access Control Pattern: `recorded_by`

Most data retrieval functions include a `recorded_by` parameter that specifies which identity is requesting the data. These functions enforce access control by:
- Verifying ownership (e.g., only return YOUR private key, not another peer's)
- Checking group membership (e.g., only return keys for groups you belong to)
- Filtering by valid_events (e.g., only return data this peer has recorded)

**Functions requiring access control:**
- `peer.get_private_key(peer_id, recorded_by, db)` - verifies recorded_by == peer_id
- `peer.get_public_key(peer_id, recorded_by, db)` - verifies ownership
- `group.pick_key(group_id, recorded_by, db)` - verifies group access
- `key.get_key(key_id, recorded_by, db)` - verifies key access
- All projection and list functions (e.g., `list_messages()`, `list_all_groups()`)

### Safe Functions Without `recorded_by`

**`key.get_peer_id_for_key()` and `key.get_peer_ids_for_key()`** intentionally lack `recorded_by` because they're used for **routing**, not access control:
- Called by `sync.unwrap_and_store()` to determine which local peer(s) can decrypt incoming blobs
- The key_id comes from network data (blob headers), not user input
- They don't expose private data TO the caller - they determine WHO should receive data
- Used internally for multi-peer support (when multiple identities share the same key)

These functions are safe because they're part of the system's internal plumbing for routing encrypted events to the correct local identities.

## Admin Group Design

Networks include two groups:
- **all_users group**: Contains all network members
- **admins group**: Contains administrative users with permission to add/remove admins

**Visibility:** The admins group is **readable by all users** (group key is shared with all joiners). This allows any user to see who the admins are, but only existing admins can modify admin membership (enforced during `group_member.create()` projection).

**Group Membership:** Only admins can add members to groups. This applies to:
- The all_users group (adding new members to the network)
- The admins group (making other users admins)
- Any custom groups created within the network

Non-admin members cannot add other users to any group. The admin check is enforced via the `group_member.validate()` function which is called by both `create()` and `project()` to ensure consistency.

**Peer Removal Authorization:** Only admins can remove peers (devices). This prevents non-admin peers from rotating group keys to exclude other members from group messages. When a peer is removed, group keys are automatically rotated for all groups the removed peer's user belonged to, ensuring the removed peer cannot decrypt future messages. **Future work:** Consider delegating peer removal decisions to a decentralized consensus mechanism or relaxing the admin-only requirement with additional safeguards.

**Future work:** Admins will need a private communication channel. Consider adding a private admin-only group separate from the visibility-tracking admins group.

