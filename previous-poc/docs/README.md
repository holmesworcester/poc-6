# Envelope-centric design with Handlers and Filter-based Subscriptions

My previous handlers-only approach ran into severe friction in these areas:

1. Blocking events with missing dependencies and unblocking when they arrived
2. Using a SQL database and switching between a SQL database and an in-memory dict
3. Handling real crypto operations and testing them

I'm anticipating even more friction once I start thinking about transit-layer encryption where there are multiple steps that events go through. So much functionality will be crammed into the sync-request handler and the process_incoming job for adding and removing transit-layer and event-layer encryption. 

I'm curious if the following design will be simpler:

1. An eventbus that anything can emit() to
2. Envelopes that carry event-related data.
3. Handlers that subscribe to envelopes (emitted to the eventbus) with certain traits based on a filter.

### Rules

- No protocol-specific fields in `/core`: protocols define everything.
- No dict or in-memory persistence for anything: all data is stored in SQL, compatible with their being 10M envelopes.
- Self-QA the runner by running it in CLI and reading output (later we will hook this up to a demo.py TUI app)
- The only envelope id is encrypted_event_id (blake2b of hash of event ciphertext). Many envelopes (transit e.g.) will not have id's for some part of their lifecycle.  
- No protocol event types should read or write the db ever. Projectors just emit deltas and framework handles it. Everything in event types is a pure function.
- Commands are pure functions that emit envelopes with dependency declarations, not with resolved data
- Event-type functions are pure functions that transform envelopes without database access
- Handlers are pure functions that can maintain their own DB tables, but not write to the projected state DB table
- Handlers have a queue and only run one operation at a time, so there is no blocking on their database
- If a command needs to create events of multiple types, like when making a new identity/user/peer, that's fine, but each event type should handle its own projection. 

### Flows: At a Glance

- Implemented in `events/<type>/flows.py` using `@flow_op`.
- Orchestrate by calling `FlowCtx.emit_event(...)` (sequentially; no placeholders), and may run read-only queries.
- Never write to the DB directly; handlers + pipeline do dep resolution, signing, validation, encryption, projection, and storage.
- Motivation: keep orchestration simple/pure and push side effects to the pipeline; filters (not a hardcoded order) drive behavior.

### Database Access Rules

1. **Event Type Functions (Commands, Validators, Projectors) - NO Database Access**
   - Must be pure functions without any database operations
   - Commands: `(params: dict[str, Any]) -> Envelope`
   - Validators: `(envelope: Envelope) -> bool`
   - Projectors: `(envelope: Envelope) -> list[Delta]`
   - Decorators enforce this with runtime checks for database access patterns

2. **Handlers - Read/Write Database Access**
   - Only handlers can modify the database
   - Access database through the `db: sqlite3.Connection` parameter
   - Examples: resolve_deps reads events, project handler applies deltas

3. **Queries - Read-Only Database Access**
   - Use `@query` decorator which provides `ReadOnlyConnection`
   - Cannot execute INSERT, UPDATE, DELETE, CREATE, DROP, ALTER operations
   - First parameter must be `db: ReadOnlyConnection`
   - Used by API endpoints and application logic to read projected state
   - Universal seen gating: all non-system queries require an actor (peer_id) and gate results per identity using local-only `seen_local` events stored in the Event Store (O(1) check via computed `seen_local_id`)

4. **Enforcement Mechanisms**

### Jobs and Reflectors

The system includes two patterns for background processing:

**Jobs** - Scheduled tasks that run periodically and maintain state:
- Signature: `(state: Dict, db: ReadOnlyConnection, time_now_ms: int) -> (success: bool, new_state: Dict, envelopes: List[Dict])`
- Examples: `sync_request_job` sends periodic sync requests to peers
- Triggered by `run_job` envelopes from the scheduler

**Reflectors** - Event-triggered functions that respond to specific events:
- Signature: `(envelope: Dict, db: ReadOnlyConnection, time_now_ms: int) -> (success: bool, envelopes: List[Dict])`
- Examples: `sync_response_reflector` responds to incoming sync requests with events
- Triggered by specific event types they subscribe to

Both patterns query the database and emit envelopes, but jobs maintain state between runs while reflectors are stateless.
   - `@command`, `@validator`, `@projector` decorators check source code for DB access
   - `ReadOnlyConnection` wrapper prevents write operations in queries
   - Runtime errors if event functions attempt database access
   - Type system enforces correct signatures 

# Envelope Structure and Dependencies

## Identity & Gating (Universal)
- All non-system API and query operations require a doer `peer_id` (or `identity_id`).
- The framework normalizes this at the boundary (API, flow, query registries) so both keys are present in params.
- Query-time visibility (when enabled): non-system queries that need to scope results per identity can gate via the `seen_events` table populated by the Event Store: `seen_events(identity_id, event_id, seen_at)`.
- Event Store records seen for the appropriate identity when storing canonical events. The receiver identity is provided on the envelope as `to_peer` (a `peer_secret_id`) by NetworkGate (derived from transit/prekey deps). Event Store trusts this and does not infer from `peer_id` or `created_by`.

## Envelope Fields
An envelope is a container for event data and metadata used by the pipeline. Key fields include:

- `event_plaintext`: The actual event data (before encryption)
- `event_blob`: Encrypted event bytes (0x01 | key_id(16) | nonce(24) | ct)
- `event_type`: Type of the event (e.g., "message", "identity", "group")
- `event_id`: Blake2b hash of event ciphertext
- `self_created`: Boolean indicating if this was created locally
- `peer_id`: The identity that created/signed the event
- `network_id`: Which network this event belongs to
- `deps`: Array of dependency references (e.g., `["identity:abc123", "key:xyz789"]`)
- `resolved_deps`: Object containing fetched dependency data
- `local_metadata`: Local-only data (never sent over network)
- Pipeline state flags: `deps_included_and_valid`, `sig_checked`, `validated`, etc.

## Dependency Resolution

Commands declare dependencies they need in the `deps` array:
```python
envelope = {
    "event_plaintext": {"type": "message", "content": "Hello"},
    "event_type": "message",
    "self_created": True,
    "peer_id": "identity_abc123",
    "deps": ["identity:identity_abc123"]  # Need identity for signing
}
```

The `resolve_deps` handler:
1. Reads the `deps` array
2. Fetches each dependency from validated events
3. Adds them to `resolved_deps`:
```python
envelope["resolved_deps"] = {
    "identity:identity_abc123": {
        "event_plaintext": {"type": "identity", "peer_id": "..."},
        "local_metadata": {"private_key": "..."}  # Included for identities
    }
}
```

Subsequent handlers can then use resolved dependencies as pure functions without database access.

## Local Metadata

`local_metadata` is envelope data that:
- Contains sensitive local information (e.g., private keys)
- Is never sent over the network
- Is included when resolving dependencies for local operations
- Is stored locally but stripped by the strip_for_send handler before transmission

# Pipelines

Handlers use filters to subscribe to the eventbus. We use these to create pipelines. Each handler transforms envelopes by adding, modifying, or removing fields.

Note: Handler load order is deterministic but arbitrary. The framework loads handler modules in alphabetical order; actual processing flow is governed by each handler’s filter, not by a hardcoded pipeline sequence. If an envelope matches a handler’s filter, that handler will process it, regardless of module load order.

## Incoming Pipeline: Network → Validated Storage

Note: an alternate version of this would let framework store and return events, fetch valid deps, and block/unblock deps.

### 1. receive_from_network
- **Input Type:**
  ```typescript
  interface NetworkData {
    origin_ip: string
    origin_port: number
    received_at: number
    raw_data: bytes
  }
  ```
- **Output Type:**
  ```typescript
  interface TransitEnvelope {
    transit_secret_id: string
    transit_ciphertext: bytes
    origin_ip: string
    origin_port: number
  }
  ```
- **Filter:** Has `origin_ip`, `origin_port`, `received_at`, `raw_data`
- **Transform:** Parses raw_data to extract transit encryption fields

### 2. resolve_deps (First Pass)
- **Input Type:**
  ```typescript
  interface NeedsDepsEnvelope {
    deps?: string[]
    deps_included_and_valid?: boolean | false
    unblocked?: boolean
    [key: string]: any
  }
  ```
- **Output Type:**
  ```typescript
  interface ResolvedDepsEnvelope {
    deps_included_and_valid: true
    resolved_deps: Record<string, ValidatedEvent>
    [key: string]: any
  } | {
    missing_deps: true
    missing_dep_list: string[]
    [key: string]: any
  }
  ```
- **Filter:** `deps` exists AND (`deps_included_and_valid` is false OR `unblocked: true`)
- **Notes:**
  - Only resolves from already-validated events. Keeps resolved_deps in envelope for reuse.
  - Owns the presence-only `validated_events` index: listens for `validated: true` envelopes and indexes their `event_id`, then unblocks dependents.

### 3. transit_crypto_handler
- **Input Type:**
  ```typescript
  interface TransitEncryptedEnvelope {
    deps_included_and_valid: true
    transit_secret_id: string
    transit_ciphertext: bytes
    resolved_deps: Record<string, ValidatedEvent>
    [key: string]: any
  }
  ```
- **Output Type:**
  ```typescript
  interface EventEncryptedEnvelope {
    network_id: string
    key_secret_id: string
    event_blob: bytes
    event_id: string  // blake2b hash of (nonce||ct) inside event_blob
    write_to_store: true
    [key: string]: any
  }
  ```
- **Filter:** `deps_included_and_valid: true` AND has `transit_secret_id` AND `transit_ciphertext` AND NOT `key_secret_id`
- **Transform:** Uses transit key from resolved_deps to decrypt
- **Emits:** Second envelope with `write_to_store: true`

### 4. remove (Optional)
- **Input Type:**
  ```typescript
  interface EventWithId {
    event_id: string
    should_remove?: boolean
    event_type?: string
    [key: string]: any
  }
  ```
- **Output Type:** Same envelope with `should_remove: false` OR drops envelope
- **Filter:** Has `event_id` AND `should_remove` is not false
- **Action:** Calls all Removers for event type

### 5. event_crypto_handler
- **Input Type:**
  ```typescript
  interface EncryptedEvent {
    deps_included_and_valid: true
    should_remove: false
    key_secret_id: string
    event_blob?: bytes
    resolved_deps: Record<string, ValidatedEvent>
    [key: string]: any
  }
  ```
- **Output Type (Key Event):**
  ```typescript
  interface UnsealedKeyEvent {
    event_type: "key"
    key_id: string
    unsealed_secret: bytes
    group_id: string
    write_to_store: true
    [key: string]: any
  }
  ```
- **Output Type (Regular Event):**
  ```typescript
  interface DecryptedEvent {
    event_plaintext: object
    event_type: string
    write_to_store: true
    [key: string]: any
  }
  ```
- **Filter:** 
  - Decrypt: `deps_included_and_valid: true` AND `should_remove: false` AND `key_secret_id` exists AND no `event_plaintext`
  - Encrypt: `validated: true` AND has `event_plaintext` AND no `event_blob`
- **Note:** Combined handler for encryption, decryption, and key unsealing

### 6. event_store
- **Input Type:**
  ```typescript
  interface StorableEvent {
    write_to_store: true
    event_id?: string
    event_blob?: bytes
    event_plaintext?: object
    key_id?: string
    [key: string]: any
  }
  ```
- **Output Type:** Same with `stored: true`
- **Filter:** `write_to_store: true`
- **Action:** Stores event data in database

### 7. resolve_deps (Second Pass for Signature)
- **Input/Output Types:** Same as first pass
- **Filter:** `event_plaintext` exists AND `sig_checked` is not true AND (`deps_included_and_valid` is false OR needs peer resolution)
- **Note:** Resolves peer_id from plaintext to get public key for signature verification

### 8. signature_handler
- **Input Type:**
  ```typescript
  interface SignableOrVerifiableEvent {
    event_plaintext: object & { signature?: bytes, peer_id?: string }
    sig_checked?: boolean
    self_created?: boolean
    deps_included_and_valid: true
    resolved_deps: Record<string, ValidatedEvent>
    [key: string]: any
  }
  ```
- **Output Type:** Same with `sig_checked: true` or `error: string`
- **Filter:** 
  - Sign: `self_created: true` AND `deps_included_and_valid: true` AND no signature
  - Verify: `event_plaintext` exists AND `sig_checked` is not true AND `deps_included_and_valid: true`
- **Note:** Combined handler for both signing and verification

### 9. check_group_membership (If Applicable)
- **Input Type:**
  ```typescript
  interface GroupEvent {
    event_plaintext: object & { group_id?: string, group_member_id?: string, user_id?: string }
    is_group_member?: boolean
    [key: string]: any
  }
  ```
- **Output Type:** Same with `is_group_member: true`
- **Filter:** event_plaintext has `group_id` AND `is_group_member` is false/absent
- **Validates:** group_member_id matches user_id and group_id

### 10. validate
- **Input Type:**
  ```typescript
  interface ValidatableEvent {
    event_plaintext: object
    event_type: string
    sig_checked: true
    is_group_member?: true
    resolved_deps?: Record<string, ValidatedEvent>
    [key: string]: any
  }
  ```
- **Output Type:** Same with `validated: true` or `error: string`
- **Filter:** Has `event_plaintext`, `event_type`, `sig_checked: true`, AND (no `group_id` in plaintext OR `is_group_member: true`)
- **Action:** Calls event type specific validator with full envelope
- **Notes:** Pure validator (no DB writes); resolve_deps maintains the `validated_events` index and unblocks dependent events.

### 11. unblock_deps
- **Input Type:**
  ```typescript
  interface ValidatedOrBlockedEvent {
    validated?: true
    missing_deps?: true
    event_id?: string
    [key: string]: any
  }
  ```
- **Output:** Emits previously blocked events with `unblocked: true`
- **Filter:** `validated: true` OR `missing_deps: true`
- **Action:** Updates SQL table of blocked events

### 12. project (Per Event Type)
- **Input Type:**
  ```typescript
  interface ProjectableEvent {
    validated: true
    event_type: string
    event_plaintext: object
    [key: string]: any
  }
  ```
- **Output Type:**
  ```typescript
  interface ProjectedEvent extends ProjectableEvent {
    projected: true
    deltas: Delta[]
  }
  ```
- **Filter:** `validated: true` AND matching event_type
- **Action:** Calls event type specific projector

## Creation Pipeline: Command → Storage

### Phase 1: Creation and Validation

### 1. Command (e.g., create_message)
- **Input Type:**
  ```typescript
  interface MessageParams {
    content: string
    channel_id: string
    identity_id: string  // Which identity to use for signing
  }
  ```
- **Output Type:**
  ```typescript
  interface CommandEnvelope {
    event_plaintext: {
      type: "message"
      content: string
      channel_id: string
      peer_id: string  // Included in event data
    }
    event_type: "message"
    self_created: true
    deps: string[]  // ["identity:identity_abc123"]
  }
  ```
- **Note:** peer_id is included in event_plaintext as it's part of the event data

### 2. resolve_deps
- **Input/Output Types:** Same as incoming pipeline
- **Note:** Resolves identity with private key in local_metadata. Keeps resolved_deps in envelope for reuse by subsequent handlers.

### 3. sign
- **Input Type:**
  ```typescript
  interface SignableEnvelope {
    event_plaintext: object & { peer_id: string }
    self_created: true
    deps_included_and_valid: true
    resolved_deps: Record<string, ValidatedEvent>
    [key: string]: any
  }
  ```
- **Output Type:**
  ```typescript
  interface SignedEnvelope extends SignableEnvelope {
    event_plaintext: object & { signature: bytes }
  }
  ```
- **Filter:** `self_created: true` AND `deps_included_and_valid: true`
- **Transform:** Uses private key from resolved_deps["identity:..."] to sign
- **Note:** No need for self_signed flag - check_sig can verify using peer_id from plaintext

### 4-9. Validation Flow
- Same handlers as incoming: check_sig → check_group_membership → validate

### Phase 2: Storage (After Validation)

### 10. encrypt_event
- **Input Type:**
  ```typescript
  interface ValidatedPlaintext {
    validated: true
    event_plaintext: object
    event_blob?: undefined
    [key: string]: any
  }
  ```
- **Output Type:**
  ```typescript
  interface EncryptedEvent {
    event_blob: bytes
    key_secret_id: string
    write_to_store: true
    [key: string]: any
  }
  ```
- **Filter:** `validated: true` AND no `event_blob`
- **Transform:** Encrypts plaintext to `event_blob`; id is assigned later by id_assign from the blob

### 11. event_store
- Same as incoming pipeline

### 12. project
- Same as incoming pipeline

## Outgoing Pipeline: Storage → Network

### 1. Outgoing Command (e.g., send_sync_request)
- **Input Type:**
  ```typescript
  interface SendParams {
    event_id: string      // Event to send
    peer_id: string       // Destination peer
    due_ms?: number       // When to send (optional)
  }
  ```
- **Output Type:**
  ```typescript
  interface OutgoingEnvelope {
    is_outgoing: true
    deps: string[]  // Dependencies to resolve
    deps_included_and_valid: false
  }
  ```
- **Transform:** Creates envelope with deps for event, address, keys, etc.

### 2. resolve_deps
- **Input/Output Types:** Same as incoming pipeline
- **Transform:** Resolves all outgoing dependencies:
  - `address:address_id` → dest_ip, dest_port
  - `event:event_id` → event_plaintext, event_blob
  - `transit_secret:transit_secret_id` → transit_secret
  - `peer:peer_id`, `user:user_id` → peer/user data

### 3. check_outgoing
- **Input Type:**
  ```typescript
  interface OutgoingWithDeps {
    is_outgoing: true
    deps_included_and_valid: true
    resolved_deps: Record<string, ValidatedEvent>
    outgoing_checked?: undefined
    [key: string]: any
  }
  ```
- **Output Type:** Same with `outgoing_checked: true`
- **Filter:** `is_outgoing: true` AND `deps_included_and_valid: true` AND no `outgoing_checked`
- **Validates:** address, peer, and user all match and are consistent

### 4. encrypt_event (If Needed)
- **Input/Output Types:** Same as creation pipeline
- **Filter:** `outgoing_checked: true` AND no `event_blob`
- **For:** Newly created events being gossiped

### 5. transit_wrap
- **Input Type:**
  ```typescript
  interface OutgoingEncrypted {
    outgoing_checked: true
    event_blob: bytes
    transit_secret_id: string
    resolved_deps: Record<string, ValidatedEvent>  // Contains transit_secret
    [key: string]: any
  }
  ```
- **Output Type:**
  ```typescript
  interface TransitEncrypted {
    transit_ciphertext: bytes
    transit_secret_id: string
    dest_ip?: string
    dest_port?: number
    due_ms?: number
  }
  ```
- **Filter:** `outgoing_checked: true` AND has `event_blob` AND `transit_secret_id`
- **Transform:** Encrypts with transit key, removes all plaintext/secrets

### 6. strip_for_send
- **Input Type:**
  ```typescript
  interface PreStrippedEnvelope {
    transit_ciphertext: bytes
    transit_secret_id: string
    dest_ip?: string
    dest_port?: number
    due_ms?: number
    [key: string]: any
  }
  ```
- **Output Type:**
  ```typescript
  interface StrippedEnvelope {
    transit_ciphertext: bytes
    transit_secret_id: string
    due_ms: number
    dest_ip: string
    dest_port: number
    stripped_for_send: true
  }
  ```
- **Filter:** Has `transit_ciphertext`
- **Validates:** Not a secret event type (identity_secret, transit_secret)

### 7. send_to_network
- **Input Type:** `StrippedEnvelope`
- **Filter:** `stripped_for_send: true`
- **Action:** Sends to network using framework's send() function

## Key Design Principles

1. **deps_included_and_valid** resets to false when any handler adds new dependencies or resolve_deps scans for missing in its filter!
2. **write_to_store: true** triggers storage at multiple points in the pipeline
3. **Handlers are pure functions** that only transform envelopes
4. **Event IDs** are blake2b hashes of ciphertext, not plaintext
5. **Transit encryption** wraps event encryption for forward secrecy
6. **Local metadata** (like private keys) stays local and is never sent

## Network Simulator

- **Filter:** `stripped_for_send: true`
- **Action:** Simulates network latency and routes back to receive_from_network
- **Note:** Requires network design that can differentiate incoming data and route to proper networks/identities

# Event Types

Each event type is a self-contained module with these components:

## Folder Structure

```
protocols/quiet/
├── __init__.py
├── api.py               # Protocol API exposure (flows/queries)
├── jobs.py              # Protocol-level jobs (flow ops)
├── reflectors.py        # Protocol-level reflector mappings
├── openapi.yaml         # API specification
├── handlers/            # Pipeline handlers
│   ├── __init__.py
│   ├── check_sig.handler.py
│   ├── decrypt_event.handler.py
│   ├── decrypt_event.schema.sql    # Handler's SQL tables
│   ├── project.handler.py
│   ├── resolve_deps.handler.py
│   └── validate.handler.py
├── events/              # Event type modules  
│   ├── message/
│   │   ├── __init__.py
│   │   ├── flows.py         # message.create flow
│   │   ├── projector.py     # project() function
│   │   ├── queries.py       # get_messages() etc.
│   │   ├── validator.py     # validate() function
│   │   └── message.sql
│   ├── identity/
│   │   ├── __init__.py
│   │   ├── flows.py         # identity.create, identity.create_as_user
│   │   ├── projector.py
│   │   ├── queries.py
│   │   ├── validator.py
│   │   └── identity.sql
│   └── (other event types...)
└── tests/
    ├── handlers/
    │   └── test_*.py
    └── events/
        ├── message/
        │   ├── test_commands.py
        │   ├── test_projector.py
        │   └── test_validator.py
        └── (mirror event structure)
```

## Components

1. **Flow** (operation): Orchestrates emit + query and returns a shaped result
   - Input: Params dict via API
   - Uses `FlowCtx.emit_event(...)` to emit events through pipeline
   - May perform read-only queries for convenience data
   - Returns `{ 'ids': {...}, 'data': {...} }`

2. **Validator**: Pure function that validates event structure
   - Input: Full envelope data and all (and only!) valid deps complete `deps` array (validators often need depency events)
   - Output: Boolean (valid/invalid)
   - Checks required fields, formats, and business rules
   - Does not check signatures and typically does not check group membership (handled by pipeline)

3. **Projector**: Pure function that converts events to database deltas
   - Input: Validated envelope
   - Output: Array of delta operations
   - Deltas specify database operations: `{"op": "insert", "table": "messages", "data": {...}}`
   - Never executes database operations directly

4. **Schema**: SQL table definitions for this event type
   - Located in `schema.sql` in the event type folder
   - Defines tables that projector deltas will populate
   - Example: messages table, message_reactions table, etc.

5. **Query** (reader): Functions that read projected state
   - Input: Query parameters
   - Output: Requested data
   - Directly queries SQL tables defined in schema.sql
   - Used by API endpoints and application logic

6. **Remover**: Pure function for cascading deletions
   - Input: Event ID and deletion context
   - Output: Boolean (should remove this event)
   - Determines if an event should be removed based on other removals
   - Example: Remove messages when their channel is removed

# API and Commands (Flows)

## API Design

The API exposes high-level operations that map to flows:
- POST `/messages` → `message.create`
- POST `/groups` → `group.create`  
- POST `/invites` → `invite.create`
- POST `/users/join` → `user.join_as_user`

API requests include:
- User intent parameters (e.g., message content, channel ID)
- Identity context (which identity is performing the action)
- Never include private keys or low-level crypto details

## Operation Interface

Operations accept parameters that mirror API requests and orchestrate event creation. They return a standard response shape:
- `ids`: Mapping of event_type to event_id for emitted events (one per type when applicable)
- `data`: Optional query-shaped payload after events are stored

Flows emit via a core helper and never write to the DB directly; the pipeline performs dependency resolution, signing, validation, encryption, projection, and storage.

### Multi-Event Flows (Sequential Emission)

For multi-step operations (e.g., `join_as_user` creating identity, peer, and user), flows emit events sequentially and pass real IDs between steps. There is no placeholder mechanism.

- Step 1 emits identity and receives `identity_id`.
- Step 2 emits peer using `identity_id` and receives `peer_id`.
- Step 3 emits user using `peer_id` and invite context, and receives `user_id`.

Flows return a standard `{ ids, data }` shape; the pipeline derives and stores event IDs.

The framework:
1. Routes API calls to appropriate commands
2. Runs handlers in sequence via the event bus
3. Provides crypto functions (sign, decrypt, etc.) to handlers
4. Provides apply() function that executes deltas on the SQL database

Handlers in the pipeline:
- `resolve_deps` handler fetches dependencies (including identity with private key)
- `sign` handler signs events using resolved identity from resolved_deps
- Other handlers perform validation, encryption, storage, etc. 

# Crypto

The framework provides crypto functions hash, kdf, encrypt, decrypt, sign, check_sig, seal, unseal that all handlers and event type commands, projectors, etc. can use. 

# Data

Handlers define and maintain their own SQLite tables and indexes which can be used by other handlers. Ownership clarification:
- Event Store (ES): stores canonical events (blob + visibility) keyed by `event_id`.
- Resolve Deps: maintains the presence-only `validated_events` index (IDs only) and unblocks dependents when IDs are validated.
- Validate: pure function; does not write indexes or the DB. It marks `validated: true` and triggers a resolve pass.

We have a protocol-defined event_store handler that stores `event_id`, `ciphertext`, `key_id`, `plaintext` for all events and emits envelopes with `stored:True` 

# Testing

Prefer scenario tests that use the API (`APIClient`) and assert on projected state via queries. For unit tests, test handlers and projectors with controlled envelopes. There is no placeholder mechanism; flows emit sequentially and provide real IDs.

## Pure Function Testing (No Database Access)

### Event Type Components:
- **Command**: Given params, verify correct envelope structure and deps array
  - Test envelope contains correct event_type, event data, and dependency declarations
  - No signature verification (signing happens in pipeline)
  - Example: `create_message({"content": "Hi", "channel_id": "123"})` returns envelope with `deps: ["identity:abc123"]`

- **Validator**: Test acceptance/rejection of event structures
  - Valid events return True
  - Invalid events (missing fields, wrong types) return False
  - No database lookups or external validation

- **Projector**: Given envelope, verify correct delta operations
  - Test delta structure: `{"op": "insert", "table": "messages", "data": {...}}`
  - Multiple deltas for complex projections
  - Pure transformation of event to database operations

- **Remover**: Given removal context, verify cascade logic
  - Returns True for events that should be removed
  - Example: `should_remove(message_id, {"removed_channels": ["chan123"]})` returns True

### Handlers:
- **Filter Test**: Verify handler processes correct envelopes
  - Handler ignores envelopes missing required fields
  - Handler processes envelopes matching its filter

- **Transformation Test**: Verify correct envelope transformation
  - Input envelope + resolved_deps → output envelope
  - Example: sign handler with identity in resolved_deps adds signature to event

## Integration Testing (With Database)

- **Query**: Test with real SQLite database
  - Apply deltas to fresh database
  - Verify query results match expected
  - Test complex queries with joins

- **End-to-end**: Full pipeline execution
  - Command → resolve_deps → sign → validate → project
  - Verify final database state 

Testing infra-specific handlers:

- **send_to_network:** must provide correct params to send(params) function for a given envelope

Testing infra

- **apply(deltas):** given a db state and deltas, the final state is correct (use db seeder and db snapshots so that states can be expressed, compared as json)
- **receive(raw_network_data):** given some unit of raw network data from an ip and port, creates envelopes with `origin_ip`, `origin_port`, `received_at`, and `raw_data`
- **crypto:** basic functional tests of all crypto functions

# Type Safety

## Design Goals
- Flexible envelope type that can carry any event data through the pipeline
- Strict type checking for handler functions and event-specific operations
- Runtime validation at handler boundaries

## Implementation

### Envelope Types
- Base `Envelope` remains a flexible `TypedDict(total=False)` allowing any fields
- Specialized envelope types document required fields at each pipeline stage:
  - `NetworkEnvelope`: Raw network data (`origin_ip`, `origin_port`, `raw_data`)
  - `TransitEnvelope`: Transit encrypted (`transit_secret_id`, `transit_ciphertext`)
  - `DecryptedEnvelope`: Decrypted event (`event_plaintext`, `event_type`, `peer_id`)
  - `ValidatedEnvelope`: Validated event (`sig_checked: True`, `validated: True`)

### Handler Typing
- All handlers accept and return base `Envelope` type for flexibility
- Runtime validation using `validate_envelope_fields()` at handler entry points
- Type guards in filter functions narrow envelope types
- Cast functions provide safe runtime type conversion with validation

### Event-Specific Types
- Event data types defined as `TypedDict` classes (e.g., `MessageEventData`, `IdentityEventData`)
- Registry maps event types to their data structures
- Commands use typed parameter classes (e.g., `MessageParams`)
- Validators/projectors use generic protocols with event-specific type parameters

### Function Signatures
- `CommandFunc`: `(params: dict[str, Any]) -> Envelope`
- `ValidatorFunc`: `(envelope: Envelope) -> bool`
- `ProjectorFunc`: `(envelope: Envelope) -> list[Delta]`
- `HandlerFunc`: `(envelope: Envelope) -> Envelope | None`
- All functions use runtime validation to ensure required fields exist

### Runtime Validation
- `validate_envelope_fields(envelope, required_fields)`: Check required fields exist
- `cast_envelope(envelope, target_type)`: Safe cast with validation
- Type guards in handler filters ensure correct envelope shape
- Validation errors add `error` field rather than throwing exceptions

### Migration Strategy
1. Keep existing handlers working with base `Envelope` type
2. Add runtime validation to all handlers
3. Document expected envelope shapes in docstrings
4. Migrate all code

## Architectural Decisions

### Peer-First Event Creation

We use a **peer-first architecture** where peer events are created before networks. This eliminates special cases in the signature handler and provides consistent event flow:

1. **Peer Creation**: When an identity wants to participate in the protocol, it first creates a peer event containing its public key. The peer_id becomes the protocol-level identifier for this identity.

2. **Network Creation**: Networks require a peer_id as creator, establishing clear ownership and ensuring the creator has a verifiable identity in the protocol.

3. **Frontend Responsibility**: The frontend manages the mapping between core identities and their peer events, passing peer_id directly to all flows.

This approach avoids:
- Complex database lookups in commands to find the right peer for an identity+network combination
- Special cases in the signature handler for network creation (where peer doesn't exist yet)
- Ambiguity about which identity should sign an event

### Core Identity vs Protocol Events

**Identity is part of the core framework**, not stored in the event store. This separation exists because:

1. **Bootstrapping**: The protocol needs identities with private keys to sign the very first events (like peer creation). If identities were events, we'd have a circular dependency.

2. **Security**: Private keys are local-only data that should never be transmitted or stored in the event log.

3. **Framework Feature**: Multiple protocols can use the same identity system without reimplementing key management.

The relationship is:
- **Core Identity**: Manages private keys, provides signing capabilities (in `/core/identity.py`)
- **Peer Event**: Protocol-level representation with public key, created by and linked to a core identity
- **Signature Handler**: Looks up core identity from peer to perform signing operations

# How to Build a Working Demo

1. decide on a project structure for event types (should it be <protocol>/event_types/projectors or projectors/type e.g.?) and for handlers (all in one /<protocol>/handlers/ folder?)
1. build a processor in core (process.py) that accepts a series of handler-defined commands with parameters.
1. build reference `params` for an event type with no dependencies (`identity` e.g.) 
1. show it can travel through the event type command, and through handlers, until it is projected and applied.
1. confirm query results contain it
1. build more reference `params` sufficient to create a network that can receive incoming data (a `key` event, a `transit_secret` event, a `network-id`, etc.)
1. complete all handlers required so that we can run a handler-defined command that generates an outgoing envelope that would trigger `send`. collect this outgoing envelope, and modify it to use it as an incoming envelope for test purposes. 
1. build a reference envelope for `receive_from_network` containing an event of a type with no dependencies (similar to the kind a sync response would send, with transit layer encryption and event layer encryption and a signature in keeping with the handler design described below)
2. show it can travel through all handlers and get projected. build each handler and prove the path through that handler at each step.
2. build another reference envelope that depends on the first, send it through first, and confirm it gets projected too after the second envelope arrives and unblocks it. (tests `resolve_deps`)
3. build a `send_sync_requests` command that sends an event of type `sync_request` to the network to test the outgoing pipeline, building any pieces along the way.
1. confirm that `network_simulator` loops it back and that it is received and applied.
1. write a real `sync_request` validator/projector that emits outgoing response envelopes and test that a command like sending a message works given enough execution steps. 
1. add the model from signed groups, with the addition of addresses and transit and event encryption, for invite, users, groups and link-invite and link events
1. begin building tests around all functionality
1. build demo that shows we can invite users, message, create channels, and generally do stuff
1. add support for blobs

Use ideal_protocol_design.md as a guide when necessary, but simplify when possible.  

Use previous_poc as a cheat sheet when implementing handler functionality related to encryption, identity, joining with `user` events, linking, etc. We can use this as a model for the kinds of handlers, though what we are building is a bit more complex because its networking model (with real addresses and transit enc) is more complex.
