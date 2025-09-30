# Framework Tests Protocol

This protocol contains the test suite for the core framework functionality. It validates the behavior of the framework components including:

- **Core Components**: tick, greedy_decrypt, handle, and test_runner
- **Handlers**: message, missing_key, and unknown event handlers
- **Cryptographic Functions**: sign/verify, encrypt/decrypt, hash, seal/unseal, KDF

## Structure

- `runner.json` - Tests for the test runner itself
- `tick.json` - Tests for the main event loop
- `handlers/` - Tests for each handler type:
  - `message/` - Message handling and creation
  - `test_crypto/` - Cryptographic function tests

## Running Tests

The test runner automatically discovers and runs all tests in this protocol when executed.

## Test Format

Tests are written in JSON format with:
- `given`: Initial state/input
- `then`: Expected output/state
- `description`: Human-readable test description

Assertions are now SQL-first. Prefer `then.tables` to check canonical SQL
tables (e.g., `messages`, `event_store`, `unknown_events`, `pending_missing_key`).
Handlers still update dict-state for legacy compatibility, but tests should
assert against tables to match framework behavior.

## TODO: Critical Concurrency & Performance Tests

Based on the database implementation requirements, the following test categories need to be added:

### Concurrency Tests
- **Slow tick + invite + join race**: Verify no stale reads and correct ordering/blocking when tick runs slowly while new events arrive
- **Multi-writer serialization**: Test concurrent `process_incoming` calls serialize properly on SQLite single writer  
- **Fresh snapshot reads**: Ensure evaluators get fresh data on each cycle, no stale cross-connection reads
- **Lease coordination**: Test tick lease prevents multiple instances from running dependency-sensitive evaluators
- **Per-item transaction isolation**: Verify `process_incoming` per-item loops maintain atomicity under load
- **Recheck marker coalescing**: Test blocked item markers properly coalesce and avoid duplicate processing

### Performance Tests  
- **10M event ingestion**: Write 10,000,000 events (~500B each) and measure commit latency staying under network speed
- **Message list pagination**: Test seek-pagination performance for last 50 messages out of large datasets
- **Concurrent read performance**: Measure read query performance with multiple concurrent connections under WAL mode
- **Bulk insert throughput**: Test batched commits (100-1000 items) vs per-item for background downloads
- **Index coverage**: Verify covering indexes prevent table lookups for critical query patterns

### File Handling Tests
- **Sequential slice writes**: Stream large file slices, verify sequential writes and integrity checks
- **Concurrent file downloads**: Test multiple file downloads serialize properly without corruption  
- **Resumable downloads**: Verify file completion markers and resumability after interruption
- **Blob slice assembly**: Test streaming assembly of large files from SQLite blob_slices

### Database Integrity Tests
- **Transaction boundary correctness**: Verify projector idempotency under replay scenarios
- **Event deduplication**: Test UNIQUE constraints properly prevent duplicate event processing
- **WAL mode behavior**: Verify READ COMMITTED isolation and writer/reader coordination
- **Busy timeout handling**: Test retry logic under SQLITE_BUSY conditions with explicit retry strategies
