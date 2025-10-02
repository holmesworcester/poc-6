# TODO:

- test unblocking on 1 peer 
- get check for deps out of crypto unwrap. 
- add 2 peer comms first
- bootstrapping (user, invite, proof of invite)
- groups (to bootstrapping too)
- group encryption
- bootstrapping
- consider other names for first_seen e.g. arrival

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

### First-Seen Pattern

The `first_seen` event type wraps references to other events and tracks:
- **WHO** saw the event (via `seen_by_peer_id`)
- **WHEN** they saw it (via `received_at`, derived from first_seen's `created_at`)
- **WHAT** event they saw (`ref_id` = referenced event ID)

This enables multi-peer support within a single client, as each peer has their own view of which events they've seen and when.

#### `seen_by_peer_id` Semantics

The `seen_by_peer_id` field identifies which peer "saw" an event, but its value depends on how the event arrived:

**For locally created events:**
- `seen_by_peer_id` = `creator_id` (the peer_id of the peer who created the event)
- The creator immediately "sees" their own event
- Example: When peer_id A creates a message, `seen_by_peer_id` = A

**For incoming transit blobs (during sync):**
- `seen_by_peer_id` = peer ID corresponding to the transit_key used for wrapping
- The transit_key is determined by the receiving peer's original sync request
- This is looked up via `network.get_peer_id_for_transit_key(hint, db)`
- Example: When peer B receives a blob wrapped with B's transit_key, `seen_by_peer_id` = B
- **Important:** `seen_by_peer_id` ≠ `creator_id` for incoming events

In the `first_seen` event itself, `created_by` equals `seen_by_peer_id`, but the source of this value differs based on the creation path.

#### Timing Semantics

All timestamps are in milliseconds since epoch.

All events in the event store have two timestamps:
- **`created_at`** (field in event data): When the event was created by its author
- **`stored_at`** (column in store table): When the event blob was stored in the local database

For different event types:
- **Message events**: `created_at` = author's creation time (may be in the past when received); `stored_at` = when stored locally
- **first_seen events**: `created_at` = `stored_at` = when this peer "saw" the referenced event (both set to the same `t_ms`)

In projections:
- **`received_at`** is extracted from `first_seen.created_at` - represents when this peer saw the referenced event
- **`created_at`** is extracted from the referenced event (e.g., message) - represents the original author's creation time
- The messages table stores both: sort/display by message's `created_at`, filter by peer via `received_at`

The `stored_at` values exist in the event store but are not typically projected into application tables. Only the `stored_at` of first_seen events is semantically meaningful per-peer (and we use `first_seen.created_at` which equals it).

### Projection Flow

1. `first_seen.project(first_seen_id, db)` is called
2. Extracts `seen_by_peer_id` from first_seen event's `created_by` field (who saw this event)
3. Extracts `received_at` from first_seen event's `created_at` field (when they saw it)
4. Projects to first_seen table: `(first_seen_id, event_id, seen_by_peer_id, received_at)`
5. Loads the referenced event and extracts its `created_at` (original author timestamp)
6. Calls event-specific projector: `message.project(event_id, db, seen_by_peer_id, received_at)`
7. Event projector inserts into its table with both timestamps: message's `created_at` (author time) and `received_at` (peer's viewing time)

### Multi-Peer Support

All projected state is scoped per-peer via `seen_by_peer_id`:
- Messages table includes `seen_by_peer_id` column
- List queries filter by `seen_by_peer_id`: `WHERE channel_id = ? AND seen_by_peer_id = ?`
- This allows a single client to manage multiple peers in the same or different networks

**List function pattern:**
- All list functions follow the pattern: `list_*(filter_params..., seen_by_peer_id, db)`
- Examples:
  - `list_messages(channel_id, seen_by_peer_id, db)` - filters by channel and peer
  - `list_all_groups(seen_by_peer_id, db)` - filters by peer only (groups aren't scoped to channels)
- The `db` parameter always comes last for consistency across the codebase

### Invite Link Design

Invite links encode 3 secrets (invite_secret, invite_key_secret, transit_secret) rather than including the full signed invite event blob.

**Tradeoff:** Smaller invite links (~400 bytes) but requires dependency checking exemption for self-created user events with invite proofs. The exemption logic in `first_seen.py` detects when a user creates their own user event (by checking if the event's peer_id references the local peer) and skips dependency checking.

**Alternative considered:** Include full signed invite event blob in link so invitee can store it locally as a dependency. This would still require an exemption (invitee doesn't have inviter's peer_shared event yet to validate the invite signature) and makes links larger (600+ bytes).

**Current solution:** Self-created user events with invite proofs skip dependency checking. Validation still occurs in `user.project()` by verifying the invite proof matches an entry in the invites table.

### Testing Requirements

- **No `time.time()` calls** - all timestamps must be explicit parameters for deterministic testing
- `received_at` comes from `first_seen.created_at` (which equals the `t_ms` passed to `store_with_first_seen()`)
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

