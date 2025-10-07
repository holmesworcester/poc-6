# NOTES:

- Bootstrap flow: Invite link contains signed invite event blob. Invitee stores it as an event, creates user event referencing it. User projection extracts metadata from invite, creates stub group/channel rows. This enables immediate messaging before sync completes.

# TODO:

- make group-member depend on the group-member of the person adding, so that missing deps will block and we can rely on our projections to be queryable synchronously. (and think about this.) --and generally review group-member and group events.
- make a safedb way to query blobs e.g. in channel.py
- reduce the number of non-subjective tables and events (break out transit keys/prekeys from group keys e.g.)
- try to use unsafedb as rarely as possible 
- have a look at invite key and prekey creation to make sure it makes sense
- is there anywhere i should *not* be using INSERT OR IGNORE? 
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

### Invite Link Design

**Architecture:** Minimal signed invite event as single source of truth for join authorization.

**Invite Event Structure:**
- Signed by inviter (Alice) using peer private key
- Contains: `invite_pubkey`, `invite_key_secret`, `group_id`, `channel_id`, `key_id`
- Stored as canonicalized JSON blob, included in invite link (~882 chars, 56% reduction from previous)

**Join Flow (Bob):**
1. Extracts invite blob from URL, stores as event → `invite_id`
2. Creates `user` event: references `invite_id`, wrapped with `invite_key` (not network key)
3. User projection: fetches invite, validates Alice's signature, extracts metadata, creates stub group/channel rows
4. Bob can message immediately (has stubs), sync fills in full details later

**Dependency Model:**
- User events depend on `invite_id` (automatic blocking/unblocking)
- Third peers: receive user before invite → block → invite arrives → unblock & project
- User projection is deterministic: same invite → same stubs for all peers

**Key Insight:** User event is the membership credential. Projection grants minimal access (stubs). Real group/channel events upgrade stubs to full data via `INSERT OR REPLACE`.

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

The protocol uses **two-layer wrapping** to separate routing authorization from content privacy:

**Layer 1: Transit Wrapping (Routing)**
- Determines WHO can store and forward an event
- Wrapped with sync request's `transit_key` (symmetric, ephemeral per sync)
- Success = routing permission granted
- Handled in `sync.unwrap_and_store()`

**Layer 2: Content Wrapping (Privacy)**
- Determines WHO can read event content
- Wrapped with group keys (symmetric) or prekeys (asymmetric)
- May fail even when transit succeeds
- Handled in `recorded.project()` via `crypto.unwrap()`

**Shareable Event Marking (Centralized in recorded.py)**

All events that successfully decrypt their content layer are **centrally marked as shareable** in `recorded.project()` (lines 151-162), regardless of whether they:
- Successfully complete projection
- Get blocked on missing semantic dependencies (e.g., missing group_id)

This centralized approach enables:
1. **Relay/forwarding of blocked events** - Peers forward messages they can't project yet (missing dependencies)
2. **Mesh network routing** - Events propagate even when individual peers can't fully process them
3. **Simpler projectors** - Type-specific code doesn't handle shareable marking

**Local-only events** (peer, key, prekey, recorded) are explicitly excluded from shareable marking and never sync to other peers.

**Special case: invite_key_shared**

The `invite_key_shared` event type handles sharing group keys with future invitees. Unlike regular `key_shared` events:
- Creator wraps the group key to an invite prekey (doesn't have the private key)
- Creator cannot decrypt their own event (expected behavior)
- Creator marks as valid without decryption (handled in `invite_key_shared.project()`)
- Recipient (invitee with private key) decrypts and adds key to local keys table

This separation eliminates the need for special-case logic in `recorded.py` and makes invite bootstrap explicit.

### Testing Requirements

- **No `time.time()` calls** - all timestamps must be explicit parameters for deterministic testing
- `recorded_at` comes from `recorded.created_at` (which equals the `t_ms` passed to `store.event()`)
- Tests can control time progression by passing explicit `t_ms` values to all event creation and storage functions

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

