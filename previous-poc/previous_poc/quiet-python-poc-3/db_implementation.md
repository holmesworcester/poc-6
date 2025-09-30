Database Implementation Plan

Goals & Targets
- Scale: Store 10,000,000 events with acceptable footprint and fast queries.
- Event size: ~500 bytes each; ingest (“download”) at network speed without DB becoming the bottleneck.
- UX: Lazy-load message lists quickly (seek-pagination), so a sender sees their message immediately after a command.
- Files: Some events carry file data; write slices sequentially as they arrive, with integrity checks and resumability.
- Concurrency: One writer at a time (SQLite), but allow concurrent reads; avoid stale reads across processes.
 - Writer coordination scope: Only dependency-sensitive evaluators (e.g., unblocking) need coordination; downloads and message appends can run concurrently and naturally serialize.

Storage Model
- KV facade policy: Prefer SQLite-native access. Do not keep a full in-memory mirror. If a KV layer exists for convenience, restrict it to tiny flags/counters and make it read-through (load on demand, invalidate on txn begin). Remove `eventStore`, `state.messages`, or any large lists from KV entirely.
- SQLite native tables for everything large/growing:
  - events (append-only event log for auditing/causality)
  - messages (materialized read model optimized for queries)
  - incoming/outgoing/deferred queues
  - blobs and blob_slices (for files)
- JSON vs columns:
  - Store indexable fields as dedicated columns (event_id, type, aggregate keys like channel_id/received_by, timestamps).
  - Payload can be TEXT/BLOB if needed, but do not rely on JSON1 indexing for core queries.
  - Event identity is protocol-specific; define dedicated columns and indexes per protocol in schema.sql. Do not bury event identity inside JSON if you need to query/deduplicate on it.

Transactions & Concurrency
- Transaction owner: `run_command` owns the transaction per command; projectors participate (`auto_transaction=False`).
- Tick as orchestrator: Discovers work and invokes jobs; each job unit runs in its own transaction (no giant tick transaction).
- Selective writer coordination: Background ingestion (downloading raw data, appending messages) can run concurrently and will naturally serialize on SQLite’s single writer. Only dependency-sensitive operations (e.g., unblocking evaluators) need coordination.
- `process_incoming`: Per-item transaction loop for safety and isolation:
  - BEGIN IMMEDIATE; pop one row from `incoming`; project; COMMIT; repeat.
  - Guarantees atomic pop+project of a single item and keeps transactions short.
- Unblock evaluation correctness:
  - Evaluators run with a fresh snapshot per cycle: start a new transaction (BEGIN IMMEDIATE), read via SQL, no stale in-memory caches.
  - When an unblocking event commits (e.g., a key added), enqueue a recheck marker/partition so blocked items are re-evaluated later even if they arrived earlier.
  - Ensure evaluators are singleton (via lease) or partitioned deterministically to avoid duplicate/unordered rechecks.
- Fresh reads: Avoid stale cross-connection reads by one (or both):
  - Prefer reading domain tables via SQL directly (no KV cache) for large data.
  - If a small KV remains, refresh it on `begin_transaction()` so every txn sees the latest committed values.
- Single tick runner for sensitive cycles: Use a DB-backed lease to ensure only one tick instance runs dependency-sensitive evaluators at a time. Other background jobs need no lease.

SQLite Settings
- PRAGMAs on connection:
  - `PRAGMA journal_mode=WAL;` (better read concurrency)
  - `PRAGMA synchronous=NORMAL;` (good perf/safety balance)
  - `PRAGMA busy_timeout = 30000;` (paired with retry)
  - `PRAGMA foreign_keys = ON;` (where used)
  - Optional: `PRAGMA cache_size = -20000;` (~20MB cache, tuneable)
- Transaction start: `BEGIN IMMEDIATE` (writer lock early, allows readers).

Schema Conventions (schema.sql)
- Protocols own schema: each `protocols/<name>/schema.sql` defines domain tables and indexes.
- Inline index lines supported by loader:
  - Inside a CREATE TABLE block, write `INDEX idx_name (col1, col2)` on its own line; the loader extracts and creates `CREATE INDEX` after the table.
- Columns first: define dedicated columns for anything you’ll query or deduplicate on. Avoid burying them in JSON.
- Timestamps: store as integer milliseconds (`created_at_ms`) for consistent ordering.
- Primary keys: use `INTEGER PRIMARY KEY AUTOINCREMENT` when you need strict monotonic IDs for seek-pagination.
- Event identity: Event ID may be protocol-specific (algorithm/format). Define its column name and uniqueness/indexing in the protocol’s schema.sql wherever needed (e.g., `event_id TEXT UNIQUE`). Do not assume a global event-id or shape in core.
 - No required monotonic ID: Protocols can rely on time-based ordering with a deterministic tie-breaker (protocol `event_id` if present, otherwise SQLite `rowid`).

Core Tables (cross-protocol)
- incoming
  - `id INTEGER PRIMARY KEY AUTOINCREMENT`
  - `blob BLOB NOT NULL` (wire blob or encoded envelope)
  - `created_at_ms INTEGER NOT NULL`
  - INDEX on `(created_at_ms)`
- deferred (optional, for auth/unblock chains)
  - `id INTEGER PRIMARY KEY AUTOINCREMENT`
  - `payload BLOB NOT NULL`
  - `idempotency_key TEXT`
  - `partition_key TEXT` (for ordered draining per aggregate)
  - `available_at_ms INTEGER NOT NULL`
  - INDEX on `(partition_key, id)` and `(idempotency_key)`
- leases
  - `lease TEXT PRIMARY KEY`
  - `owner TEXT NOT NULL`
  - `expires_at_ms INTEGER NOT NULL`
- events (optional generic event log)
  - Minimal columns: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `event_type TEXT NOT NULL`, `created_at_ms INTEGER NOT NULL`, `payload BLOB NOT NULL`.
  - Protocols that require dedup or lookups by event identity should extend with their own `event_id` (UNIQUE) and any aggregate/index keys inside their schema.sql.

Protocol Tables (example: messages)
- messages
  - `id INTEGER PRIMARY KEY AUTOINCREMENT`
  - `event_id TEXT UNIQUE` (optional; protocol decides exact event-id format and whether to enforce uniqueness here)
  - `text TEXT NOT NULL`
  - `sender TEXT NOT NULL`
  - `recipient TEXT` (optional)
  - `received_by TEXT NOT NULL` (identity/view that owns the message)
  - `timestamp_ms INTEGER NOT NULL` (from envelope or server time)
  - `sig TEXT` (if applicable)
  - `unknown_peer BOOLEAN DEFAULT 0`
  - `created_at_ms INTEGER NOT NULL`
  - Recommended indexes for paging and lookups:
    - If using time+event_id ordering: `CREATE INDEX IF NOT EXISTS idx_messages_recv_ts_eid ON messages(received_by, timestamp_ms DESC, event_id DESC);`
    - If no event_id, rely on rowid: `CREATE INDEX IF NOT EXISTS idx_messages_recv_ts ON messages(received_by, timestamp_ms DESC);`
    - Additionally: `(sender)`, `(recipient)` as needed

Files (Blobs)
- blobs
  - `file_id TEXT PRIMARY KEY` (content hash)
  - `total_size INTEGER NOT NULL`
  - `mime TEXT`
  - `completed BOOLEAN DEFAULT 0`
- blob_slices
  - `file_id TEXT NOT NULL`
  - `slice_no INTEGER NOT NULL` (0..N-1)
  - `offset INTEGER NOT NULL`
  - `data BLOB NOT NULL`
  - `PRIMARY KEY(file_id, slice_no)`
  - INDEX on `(file_id, offset)`
- Writing model: Append slices sequentially as events arrive; mark `completed=1` when the final slice is persisted; optionally stream-assemble to a file on disk if needed for large payloads.

Query Patterns
- Lazy loading messages (keyset pagination by time + tie-breaker):
  - With protocol event_id:
    - Latest page: `SELECT sender, text, timestamp_ms, event_id FROM messages WHERE received_by=? ORDER BY timestamp_ms DESC, event_id DESC LIMIT ?;`
    - Next pages: `... WHERE received_by=? AND (timestamp_ms < :ts OR (timestamp_ms = :ts AND event_id < :eid)) ORDER BY timestamp_ms DESC, event_id DESC LIMIT :n;`
  - Without event_id (use rowid as internal tie-breaker):
    - Latest: `... ORDER BY timestamp_ms DESC, rowid DESC LIMIT ?;`
    - Next: `... AND (timestamp_ms < :ts OR (timestamp_ms = :ts AND rowid < :rid)) ORDER BY timestamp_ms DESC, rowid DESC LIMIT :n;`
- Message insert:
  - If protocol uses an event identity: include `event_id` and enforce `UNIQUE` with `ON CONFLICT(event_id) DO NOTHING` for idempotency.
  - If not, omit `event_id` and rely on other keys or internal `rowid` for pagination; apply projector-level idempotency as needed.
- Event log append:
  - Same pattern; protocols decide whether to include `event_id` column and index keys for their query shapes; payload remains TEXT/BLOB for auditing.

Command & Tick Behavior
- `run_command` transaction scope per command; projectors run inside (`auto_transaction=False`).
- Command opt-out for self-managed transactions:
  - Commands like `process_incoming` may set `MANAGE_TRANSACTIONS=True` and run per-item loops with their own `begin/commit`.
  - The framework will not wrap those in an outer transaction and will not auto-apply `'db'` or `'newEvents'` from their return; the command is responsible for correctness.
- Tick:
  - Acquire/renew the `tick` lease.
  - Run job commands; for `process_incoming`, the command performs per-item transactions.
  - Drain deferred queue by selecting per partition and projecting 1 item per transaction.
  - For dependency-sensitive evaluators (e.g., unblocking), ensure singleton execution via lease or deterministic partitioning by key.

Recheck Markers (Protocol-owned)
- Responsibility: Protocols own the definition and behavior of “blocked” items and their re-evaluation. Tick only orchestrates by invoking the protocol’s job.
- Blocked table (example):
  - Columns: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `reason_type TEXT`, `reason_key TEXT`, `envelope BLOB`, `created_at_ms INTEGER`, `event_id TEXT UNIQUE`.
  - Index: `(reason_type, reason_key, id)` to fetch a partition FIFO.
- Marker queue (example):
  - Columns: `partition_key TEXT PRIMARY KEY`, `reason_type TEXT`, `available_at_ms INTEGER`, `attempts INTEGER DEFAULT 0`.
  - Semantics: One row per partition; coalesces many dependents into a single recheck pass; idempotent.
- Enqueue on unblock (projector):
  - On committing an unblocking event (e.g., key added, invite created), upsert a marker in the same transaction:
    - `INSERT INTO recheck_queue(partition_key, reason_type, available_at_ms) VALUES (?, ?, ?) ON CONFLICT(partition_key) DO NOTHING;`
  - Optionally, if a marker exists with a later `available_at_ms`, use `DO UPDATE SET available_at_ms = MIN(excluded.available_at_ms, recheck_queue.available_at_ms)` to pull it earlier.
- Drain job (handler command; MANAGE_TRANSACTIONS=True):
  - Claim marker: short tx to fetch one row and DELETE it (claim) or UPDATE status to 'processing'.
  - Process partition: SELECT blocked items for `(reason_type, reason_key)` ordered by `id`. For each item, run a per-item transaction: try `handle(envelope, auto_transaction=False)`; on success DELETE from `blocked`; if still blocked for a different reason UPDATE `(reason_type, reason_key)`; on hard error backoff/park.
  - If items remain blocked for the same key, reinsert marker with backoff by setting a future `available_at_ms`.
- Concurrency behavior:
  - Tick lease ensures exactly one drainer. Projectors can enqueue new markers while the drainer runs; those are picked up on the next loop iteration.
  - For stricter coalescing, use a status-based claim (UPDATE to 'processing') so projectors can only advance `available_at_ms` earlier rather than insert duplicates.
- Idempotency: Projectors must be idempotent; enforce UNIQUE on event identity where applicable and use upsert/ignore to make reprocessing safe.

Handler Implementation (Protocol-owned)
- Write SQL directly in handler functions for protocol tables defined in that protocol’s `schema.sql`.
- Use the connection from the framework (`db.conn`); do not open separate SQLite connections in handlers.
- Do not call `commit/rollback` in handlers; the framework (or per-item loop) owns transaction boundaries.
- Always parameterize SQL and lean on explicit indexes.
- Prefer upsert/ignore (`ON CONFLICT DO NOTHING`) and existence checks to guarantee projector idempotency.

Pure Commands (Optional Simplification)
- Convention: Commands are “pure” — they don’t mutate domain state directly. They only emit events and declare infra intents.
- Result contract (illustrative):
  - `newEnvelopes`: envelopes to project in the same transaction (small batches, UX-critical paths).
  - `perItem`: list of `{incomingId, envelope}` for per-item pop+project transactions.
  - `infra`: allowlisted intents (e.g., `incomingRemove`, `outgoingEnqueue`, `deferredEnqueue`, `blobAppendSlice`).
- Core executes intents under narrow, well-defined transactions to keep operations short and observable.

Performance & Capacity
- 10M events x 500B ≈ 5 GB payload + indexes/overhead; fits on SQLite with WAL and proper indexes.
- Inserts:
  - Keep transactions short; per-item is fine for safety. For bulk import, optionally batch multiple inserts per commit to improve throughput.
  - Use prepared statements and avoid large JSON parsing in hot paths.
  - Background downloads: can run concurrently without special leases; they serialize on the single writer. Consider batching commits (e.g., every 100–1000 rows) for higher throughput.
- Reads:
  - Use narrow SELECTs with covering indexes when possible.
  - Seek-pagination (by `id`) avoids OFFSET scans.
- PRAGMA and vacuum:
  - Periodic `ANALYZE;` and `VACUUM;` as maintenance (e.g., on version upgrade or rare admin path).

Testing & Observability
- Concurrency test: “slow tick + invite + join” race; verify no stale reads and correct ordering/blocking.
- Ingestion test: Write N (e.g., 100k) 500B events and measure commit latency and read latency for last 50.
- File test: Stream slices for a large file; verify sequential writes and final integrity.
- Add lightweight counters (rows read/written, failed inserts due to idempotency) and log slow queries (>50ms).

Idempotency (Projectors)
- Convention: All projectors must be idempotent. Re-applying the same event must not change resulting state or counters beyond the first application.
- Enforcement (schema): Prefer UNIQUE constraints on event identity columns per protocol (e.g., `event_id` or a composite) and/or existence checks in projector SQL.
- Enforcement (logic): Projectors should upsert/ignore rather than append blindly (e.g., `INSERT ... ON CONFLICT DO NOTHING`, or SELECT-before-INSERT with a unique index).
- Tests: For every projector test that applies an envelope, add an idempotency variant that applies the same envelope twice and expects identical final state. Keep JSON tests readable; the test runner can support a simple `"idempotent": true` flag or duplicate-invocation helper when we wire it.

Migration Plan (Phased)
1) SQLite settings: enable WAL, BEGIN IMMEDIATE, busy_timeout; add refresh-on-begin for the KV cache.
2) Tick lease: add `leases` table and helpers; enforce single runner.
3) Queues: move `incoming` to a table and update `process_incoming` to per-item transactions (self-managed).
4) Events/messages: create tables and indexes; update handlers to write and query via SQL directly.
5) Files: add `blobs`/`blob_slices`; implement sequential slice writes and completion marker.
6) Remove large lists from KV cache (`eventStore`, `state.messages`); keep only small aggregates in KV.
7) Tests: add concurrency, ingestion, and paging tests (keep JSON tests readable; runner handles concurrency setup).

Open Questions
- Which aggregates/keys should be first-class columns per protocol (e.g., group_id/channel_id)?
- Expected ingestion QPS for large imports; do we need a “batch import” mode that commits every M items?
- Retention policy: keep all events forever or snapshot/prune older events per protocol?
- Idempotency keys on commands: do protocols provide them even if clients don’t auto-retry?
- For files, do we also mirror to filesystem for large objects, or keep entirely in SQLite?
 - How far to reduce/remove the KV facade — is a minimal, read-through KV needed at all, or can all reads/writes be SQL-only?
