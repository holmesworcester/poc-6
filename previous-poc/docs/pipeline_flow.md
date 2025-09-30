# Pipeline Flow Documentation (DRAFT PLAN)

This document describes a potential plan for the event processing pipeline flows in the Quiet protocol implementation.

## Overview

Events flow through different pipelines depending on their origin:

- **Receive**: unwrap or drop, confirm network+peer match or drop, **Store**
- **Create**: create plaintext, sign, encrypt plaintext, **Store**
- **Store**: assign id from encrypted blob, generate "seen" event (names event, and a hash of network+peer), store both, **Resolve**
- **Resolve**: resolve_deps on hint_id, decrypt, resolve_deps on plaintext deps, **Act**
- **Act**: validate or error/drop, reflect or project or error/drop, return ids for request (if tracked i.e. if triggered by event creation flow)

- **Send**: not really a pipeline, just set outgoing_event_ids = some_flow(dest_and_transit_params) to create and store the event, then ~ map(outgoing_event_ids).send(outgoing_id, dest_and_transit_params). (Sending is rare and mostly done by sync_request jobs and reflector. Only exception is bootstrap.)

The pipeline uses handlers that process envelopes sequentially, with dependency resolution. Blocking/unblocking is protocol-dispatched (not a core first-class feature) so core remains generic and protocols define their own blocking semantics.

## Detailed Pipeline Flows

### Receive Pipeline
```
network_data → receive_from_network → resolve_deps(transit) → unwrap → network_gate → store_event
```

**receive_from_network**:
- Extracts: `transit_ciphertext`, `transit_secret_id`, `origin_ip`, `origin_port`, `received_at`
- Validates: basic format, size limits
- Drops: malformed data

**resolve_deps(transit)**:
- Purpose: Resolve the transit secret (DEM) or prekey secret (KEM) required to proceed.
- Behavior: Look up local-only secret events for `transit_secret_id` or `prekey_secret_id` and attach to `resolved_deps`.
- Non‑blocking: If missing or invalid, DROP immediately (no blocking at this stage).

**unwrap (DEM or KEM)**:
- DEM path (transit_unwrap): requires `transit_secret_id` resolved; decrypts transit layer to `event_blob` (hint_id + ciphertext); extracts `key_secret_id` from blob header; DROP on failure.
- KEM path (open_sealed): requires `prekey_secret_id` resolved; opens sealed bytes to `event_plaintext` for `sync_request` and similar; sets flags for later store/validation; DROP on failure.

**network_gate**:
- Verifies: attributed network from resolved deps matches any `network_id` in plaintext
- Sets: `network_id` on the envelope from deps if missing
- Attaches: receiver identity to the envelope as `to_peer` (peer_secret_id) and `to_peer_id` (peer_id) from transit/prekey deps
- Drops: events with mismatched attribution
- Sets: `write_to_store = true` if authorized

### Create Pipeline
```
flow_request → validate → signature → event_encrypt → store_event
```

**validate**:
- Checks: event structure, required fields, business logic
- Validates: referenced entities exist (groups, channels, etc.)
- Sets: `validated = true`
- Errors: invalid events are dropped

**signature**:
- Signs: canonical plaintext with local key
- Adds: signature to `event_plaintext`
- Sets: `sig_checked = true`

**event_encrypt**:
- Encrypts: plaintext to `event_blob` with key hints
- Requires: `key_secret_id` for encryption key
- Produces: `event_blob = 0x01 + key_id(16) + nonce(24) + ciphertext`
- Sets: `write_to_store = true`

### Store Phase (Convergence Point)
```
(Receive OR Create) → store_event → RESOLVE
```

**store_event**:
- Assigns: `event_id = blake2b(event_blob, digest_size=16).hex()` strictly from the encrypted blob
- Stores: main event; records visibility in `seen_events(identity_id=to_peer, event_id, seen_at)`
- Sets: `stored = true`
- Deduplicates: by event_id (idempotent)

### Resolve Phase
```
stored_event → resolve_deps(decrypt) → event_decrypt → resolve_deps(plaintext) → ACT
```

**resolve_deps (decrypt)**:
- Checks: `key_secret_id` available for decryption
- If missing: dispatch a protocol-level block request (persist/resume handled by protocol) and halt this run
- Resume: protocol re-enqueues when required keys become available
- Sets: `deps_included_and_valid = true` when ready

**event_decrypt**:
- Decrypts: `event_blob` to `event_plaintext` using resolved key
- Derives: `event_type` from plaintext
- Verifies: signatures on decrypted content

**resolve_deps (plaintext)**:
- Extracts: dependency references from plaintext
- Checks: all referenced events are validated
- If missing: dispatch a protocol-level block request and halt this run
- Continues: when all deps satisfied (protocol resumes processing)

### Act Phase
```
resolved_event → validate → reflect → project → response
```

**validate**:
- Verifies: plaintext structure and signatures
- Checks: business logic rules with all dependencies
- Sets: `validated = true` or marks for deletion

**reflect**:
- Checks: if reflector exists for this event type
- Generates: automatic response events (sync responses, key exchanges) if reflector present
- Triggers: additional event creation flows for generated responses
- Passes: original envelope through unchanged (may also emit additional envelopes)
- Skips: reflection entirely if no reflector registered for event type

**project**:
- Applies: state changes to local database from validated events
- Updates: derived tables, indexes, views based on event content
- Sets: `projected = true` on envelope
- Returns: event_ids for flow responses (if processing was triggered by event creation)

## Core Principles

1. **ID Assignment**: All events get deterministic IDs from their encrypted blob (`event_blob`) inside `store_event`
2. **Storage**: `store_event` stores events atomically using the assigned event_id
3. **Dependency Resolution**: Events may be blocked waiting for dependencies, then unblocked when deps arrive
4. **Projection After Storage**: Database state updates happen after storage and full dependency resolution
5. **Sending Not A Pipeline**: Sending data is a function on already-stored event_ids

## Dispatch-Only Blocking (Current Plan)

- Protocols decide when/how to block and resume; core remains agnostic.
- Middlewares dispatch `TASK` to a protocol queue (e.g., `protocol:blocking`) with deps and a resume hint, then halt.
- Protocol code persists blocked state in its own tables and re-enqueues into the post-storage chain via core `ENQUEUE`.
- Middlewares in post-storage must be idempotent and/or re-check availability to early-exit when satisfied.

## Express-Style Middleware Architecture (No Filters)

We eliminate per-handler filters in favor of fixed, ordered middleware chains.

### Core API (draft)
- Entry points:
  - `pipeline.receive(rawEnv)` — run receive chain
  - `pipeline.create(plaintextEnv)` — run create chain
- Chains converge at storage; post-storage chain runs identically.

### Middleware API (draft)
- Signature: `(env, ctx, next) => void`
- `env`: mutable envelope dict (protocol-specific fields allowed)
- `ctx`: core services (db, clock, logger) and helpers
  - `ctx.dispatch(effect)` — queue a core effect (see Effects)
  - `ctx.drop(reason?)` — stop processing for current envelope
- `next(env)`: continue to next middleware

Pseudo-code:
```
def middleware(env, ctx, next):
  try:
    if should_store(env):
      ctx.dispatch({ type: 'STORE', envelope: env })
      return  # reducer will enqueue post-storage work
    if should_drop(env):
      ctx.drop('policy_violation')
      return
    if needs_blocking(env):
      # Dispatch-only: let the protocol persist blocked state and resume later
      ctx.dispatch({
        'type': 'TASK',
        'queue': 'protocol:blocking',
        'payload': {
          'protocol': 'quiet',
          'event_hint': env.get('event_id') or env.get('event_blob'),
          'deps': ['event:abc', 'key_secret:deadbeef'],
          'resume_chain': 'post'
        }
      })
      return
    next(env)
  except KnownError as e:
    ctx.log.warn(str(e))
    ctx.drop('handler_error')
```

### Effects (core-generic)
- `STORE`: `{ envelope }` — assign id from `event_blob`, write to storage, emit post-storage work
- `ENQUEUE`: `{ chain: 'receive'|'create'|'post', envelope }` — schedule into a chain
- `DROP`: `{ event_id?, reason? }` — record and discard
- `TASK`: `{ queue: string, payload: any }` — generic work items (e.g., outgoing send or protocol-level blocking)

### Fixed Chains
- Receive (to storage): `receive_from_network → resolve_deps(transit) → unwrap → network_gate → store_event`
- Create (to storage): `validate → signature → event_encrypt → store_event`
- Post-Storage (shared): `resolve_deps(decrypt) → event_decrypt → resolve_deps(plaintext) → validate → reflect → project → response`

### Complete Event Processing:

**Created Events**: Process through create pipeline, then if stored successfully, continue through post-storage pipeline.

**Received Events**: Process through receive pipeline, then if stored successfully, continue through post-storage pipeline.

### Middleware Examples

- `store_event`: `ctx.dispatch({ type: 'STORE', envelope: env }); return`
- `resolve_deps(transit)`: attach local secrets; if missing → `ctx.drop(); return`
- `resolve_deps(decrypt)`: missing keys → `ctx.dispatch(TASK → protocol:blocking)` and return; else `next(env)`
- `network_gate`: mismatch → `ctx.drop(); return`; else `next(env)`
- `reflect`: `ctx.dispatch({ type: 'ENQUEUE', chain: 'post', envelope: responseEnv }); next(env)`
- `project`: apply state updates; `next(env)`

### Runner Sketch (draft)
```
class Runner:
  def __init__(self, chains, reducer, services):
    self.chains = chains            # { receive: [...], create: [...], post: [...] }
    self.reducer = reducer          # reduce(effects, services) -> { enqueued: Envelope[] }
    self.services = services        # db, clock, idStrategy, queues

  def run_chain(self, chain_name, env):
    effects = []
    ctx = self._make_ctx(effects)
    chain = self.chains[chain_name]
    def call(i, e):
      if i == len(chain):
        return
      return chain[i](e, ctx, lambda ee: call(i+1, ee))
    call(0, env)
    # Apply all effects atomically, then enqueue any new envelopes
    result = self.reducer(effects, self.services)
    for env2 in result.enqueued:
      self.enqueue(env2)

  def _make_ctx(self, effects):
    return {
      'dispatch': lambda eff: effects.append(eff),
      'drop': lambda reason=None: effects.append({ 'type': 'DROP', 'reason': reason }),
      'log': self.services.logger,
    }
```

### Effects Reducer (draft)
Atomic responsibilities:
- `STORE`: assign id (via `idStrategy(event_blob)`), write rows, emit post-storage `ENQUEUE` for the stored envelope and a local `seen` envelope if applicable
- `DROP`: record metrics/logs only
- `ENQUEUE`: append to in-memory queue for next iteration
- `TASK`: write to external queue (e.g., outgoing network) or protocol-level queues

Reducer is the single point where DB/queue side effects occur. Middleware stays simple and testable. Blocked-state persistence is owned by protocols that consume protocol `TASK`s.


### Error Handling:

**Pipeline Error Handling**: Centralized exception handling in Pipeline class catches specific error types.

**Event Dropping**: Middleware returns None to drop events from pipeline.

**Dependency Blocking**: DependencyMissing exceptions trigger a protocol-level blocking dispatch for later retry.

**Error Boundaries**: Pipeline class provides single point for error logging and recovery.

This approach would eliminate the current Handler/Filter complexity while making the pipeline more maintainable and debuggable.

## Effect Isolation Architecture

### Pure Pipeline with Effect Accumulation

Instead of middleware performing side effects immediately, accumulate effects during pipeline execution and process them afterward in a coordinated way.

**Pipeline Result Structure**:
- **stored_events**: List of event_ids that were stored during this run
- **validated_events**: List of event_ids that passed validation
- **block_requests**: Protocol-level block requests with deps and resume hints
- **projection_ops**: Database state changes to apply
- **dependency_checks**: Events that need dependency resolution

**Benefits of Effect Isolation**:
- **No Concurrency in Pipeline**: Individual pipeline runs are pure functions
- **Batch Effect Processing**: Central coordinator handles all side effects atomically
- **Deterministic Ordering**: Effects processed in predictable sequence
- **Easier Testing**: Pipeline behavior is deterministic and side-effect free
- **Simplified Reasoning**: No locks or races during pipeline execution

### Coordinated Effect Processing

**Single-Threaded Coordinator**:
1. **Collect Results**: Gather pipeline results from multiple concurrent runs
2. **Process Stored Events**: Update event storage tracking
3. **Process Validated Events**: Update validation state atomically
4. **Forward Block Requests**: Send protocol block requests to protocol queues
5. **Apply Projections**: Execute database state changes (read-only in core; writes via reducers)

**Dependency Resolution Without Races**:
- Pipeline runs accumulate "validated" effects and protocol "block_requests"
- Core does not manage blocked state; protocols process block queues and availability updates atomically
- Protocol managers resume events in batch, eliminating check-and-block races within the protocol domain

### Transitive Effect Handling

**Queue-First Processing (Default)**:
- No multi-round loop required for correctness.
- After availability changes, protocols enqueue follow-up work into durable queues and process them on subsequent ticks.
- With monotonic availability and idempotent handlers, transitive chains converge naturally across queue cycles.

**Bounded Local Rounds (Optional Optimization)**:
- Protocol managers MAY perform a small number of in-memory rounds under a protocol dependency lock to collapse trivial transitive unblocks.
- Use strict budgets (e.g., N=10 items or T=5–20ms) to avoid starvation; persist and queue remaining work.

**Effect Ordering**:
1. Storage effects (events get IDs and storage)
2. Validation effects (events marked as validated)
3. Protocol block request forwarding (if needed)
4. Projection effects (database state updates)

### Coordinator Implementation Strategy

**Batch Collection**:
- Accumulate pipeline results over short time windows (e.g., 10ms)
- Process batches atomically to maximize effect coordination
- Balance latency vs. coordination benefits

**Effect Deduplication**:
- Same event_id validated multiple times = single effect
- Same blocking conditions = avoid duplicate blocking (protocol side)
- Idempotent effect processing

**Failure Isolation**:
- Pipeline failures don't affect other pipelines
- Coordinator failures can replay from accumulated effects
- Core does not hold long-running locks; protocol workers do short, transactional steps

This architecture eliminates concurrency concerns from pipeline implementation while ensuring all effects are coordinated properly.

## Dependency Resolution & Concurrency

Concrete, job-driven, dispatch-only design. Pipeline emits effects; writers persist them; a protocol job manages blocking/unblocking and re-enqueues work safely.

### Effects Emitted by Middlewares
- `BLOCK { event_id, deps[], resume_chain, resume_hint, envelope_ptr }` — from `resolve_deps` when deps missing.
- `UNBLOCK { dep_type, dep_id, state }` — from `validate` when a dependency becomes available (e.g., event validated).

### Writers (Effect Reducer)
- Persist only; no complex logic.
- `STORE`: write event and `seen`; outbox‑enqueue `{chain:'post', event_id}` atomically.
- `ENQUEUE`: insert into `pipeline_queue` with dedupe `(chain,event_id,resume_hint)`.
- `BLOCK`: insert into `quiet_block_requests_queue` and `quiet_block_request_deps` (one row per dep).
- `UNBLOCK`: insert into `quiet_available_signals_queue`.
- `DROP`: metrics/logs only.

### Protocol Tables (SQLite)
```sql
CREATE TABLE IF NOT EXISTS quiet_block_requests_queue (
  event_id TEXT PRIMARY KEY,
  envelope_ptr BLOB NOT NULL,
  resume_chain TEXT NOT NULL,
  resume_hint TEXT,
  enqueued_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quiet_block_request_deps (
  event_id TEXT NOT NULL,
  dep_type TEXT NOT NULL,
  dep_id TEXT NOT NULL,
  required_state TEXT NOT NULL,
  PRIMARY KEY (event_id, dep_type, dep_id)
);
CREATE TABLE IF NOT EXISTS quiet_available_signals_queue (
  dep_type TEXT NOT NULL,
  dep_id TEXT NOT NULL,
  state TEXT NOT NULL,
  signaled_at INTEGER NOT NULL,
  PRIMARY KEY (dep_type, dep_id, state)
);
CREATE TABLE IF NOT EXISTS quiet_blocked_events (
  event_id TEXT PRIMARY KEY,
  envelope_ptr BLOB NOT NULL,
  blocked_at INTEGER NOT NULL,
  resume_chain TEXT NOT NULL,
  resume_hint TEXT
);
CREATE TABLE IF NOT EXISTS quiet_blocked_event_deps (
  event_id TEXT NOT NULL,
  dep_type TEXT NOT NULL,
  dep_id TEXT NOT NULL,
  required_state TEXT NOT NULL,
  PRIMARY KEY (event_id, dep_type, dep_id)
);
CREATE TABLE IF NOT EXISTS quiet_available_dependencies (
  dep_type TEXT NOT NULL,
  dep_id TEXT NOT NULL,
  state TEXT NOT NULL,
  available_since INTEGER NOT NULL,
  PRIMARY KEY (dep_type, dep_id, state)
);
```

### DepsJob (Protocol) — Concrete Steps
Single worker; short batches; uses `BEGIN IMMEDIATE` per batch to avoid races. Jobs match current pattern: they run DB queries and enqueue events back into the pipeline.

1) Handle BLOCK requests
```sql
-- Get batch
SELECT event_id, envelope_ptr, resume_chain, resume_hint
FROM quiet_block_requests_queue
ORDER BY enqueued_at
LIMIT :batch;
```
For each `event_id`:
```sql
BEGIN IMMEDIATE; -- dependency lock window
-- Double-check: are all deps available now?
SELECT COUNT(1) AS missing
FROM quiet_block_request_deps d
LEFT JOIN quiet_available_dependencies a
  ON a.dep_type=d.dep_type AND a.dep_id=d.dep_id AND a.state=d.required_state
WHERE d.event_id=:event_id AND a.dep_id IS NULL;
```
- If `missing=0`: delete from `quiet_block_request_*`; call `pipeline.enqueue([{chain: resume_chain, event_id, resume_hint}])`; COMMIT.
- Else: UPSERT to `quiet_blocked_events`; `INSERT OR IGNORE` deps into `quiet_blocked_event_deps`; delete from request tables; COMMIT.

2) Handle UNBLOCK signals
```sql
-- Get batch
SELECT dep_type, dep_id, state
FROM quiet_available_signals_queue
ORDER BY signaled_at
LIMIT :batch;
```
For each signal:
```sql
BEGIN IMMEDIATE;
INSERT OR IGNORE INTO quiet_available_dependencies(dep_type,dep_id,state,available_since)
VALUES(:dep_type,:dep_id,:state,:now_ms);

-- Find candidate blocked events
SELECT DISTINCT event_id
FROM quiet_blocked_event_deps
WHERE dep_type=:dep_type AND dep_id=:dep_id AND required_state=:state
LIMIT :fanout;

-- For each event_id: are ALL deps now satisfied?
SELECT COUNT(1) AS missing
FROM quiet_blocked_event_deps d
LEFT JOIN quiet_available_dependencies a
  ON a.dep_type=d.dep_type AND a.dep_id=d.dep_id AND a.state=d.required_state
WHERE d.event_id=:event_id AND a.dep_id IS NULL;

-- If missing=0: delete from blocked tables and pipeline.enqueue([{chain,resume_hint,event_id}])
DELETE FROM quiet_available_signals_queue WHERE dep_type=:dep_type AND dep_id=:dep_id AND state=:state;
COMMIT;
```

3) Enqueue API
- In-process function `pipeline.enqueue(items: list[{chain,event_id,resume_hint?}]): list[event_id]`.
- Optionally exposed as HTTP for external workers; returns accepted IDs; may take an optional serialization lock per chain.

### Correctness Guarantees
- No lost wakeups: BLOCK path re-checks availability inside the transaction; UNBLOCK path upserts availability then scans dependents within the same transaction.
- Idempotent by design: UPSERT/`INSERT OR IGNORE` with natural unique keys; `pipeline_queue` dedup on `(chain,event_id,resume_hint)`.
- Monotonic availability: `quiet_available_dependencies` only grows (stored→validated); never delete.
- Short locks: only the job uses `BEGIN IMMEDIATE`; writers never take the dependency lock.
- Simplicity: writers just write; the job is the single place that makes block/unblock decisions and re-enqueues.
