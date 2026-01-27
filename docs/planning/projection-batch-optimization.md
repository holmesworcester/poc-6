# Projection batch optimization

## Goal
Reduce projection wall time by batching DB lookups and decryption, similar to the receive path.

## Current pain points
- `recorded.project_ids()` runs per-recorded_id, doing many SQLite queries per event.
- Key lookups (`group_keys`, `group_prekeys`) are repeated for every event even when hints are identical.
- Dependency checks are often per-event queries even when the batch shares deps.

## Proposed batch flow
1) **Load a batch of recorded rows**
   - Use `ingest_index` or `recorded` stream to get `(recorded_id, event_id, recorded_by, hint, event_type)`.
   - Avoid per-event `store.get(recorded_id)`.

2) **Collapse key hints**
   - Deduplicate key hints from encrypted events.
   - Query `group_keys` and `group_prekeys` once per peer with `WHERE key_id IN (...)`.
   - Build an in-memory key cache keyed by `key_id`.

3) **Bulk fetch blobs**
   - Fetch all `event_id` blobs in one query from `store` (or a few chunked queries).

4) **Decrypt in memory**
   - Use the key cache to unwrap/decrypt without extra DB calls.
   - Track missing keys to block events in batch.

5) **Batch dependency reads**
   - For each event type, collect the dependency IDs the projector will need.
   - Query these tables with `IN (...)` and build lookup maps.

6) **Project in memory, apply writes in a transaction**
   - Apply projector logic using preloaded data.
   - Write results in batched `executemany` calls per table.

## Expected perf impact
- If batches share keys (typical per-channel/group), this should remove most DB round-trips and give a **2–5x** improvement.
- For highly diverse events, gains are smaller but still positive from reduced Python overhead.

## Should we add a separate plaintext projection job?
**Maybe, but only if we need lower latency for specific plaintext types.**

Pros:
- Plaintext events skip key lookups and decrypt, so they can be projected quickly.
- Useful for control/auth/removal events that should be visible immediately.

Cons:
- Adds scheduling complexity and two projection paths to maintain.
- Many plaintext events still have dependency checks, so benefit is limited unless we prioritize specific types.

Recommendation:
- Keep a single batched projection path, but allow **high-priority event types** to run in a smaller, frequent batch.
- If we later identify a true latency bottleneck for plaintext-only types, split them into a dedicated high-priority job.

## Minimal implementation plan
- Add a `project_batch()` path that accepts `recorded_id` list plus prefetch options.
- Add a key-cache-aware unwrap helper (no DB calls inside unwrap).
- Add batched dependency fetch helpers in v2 resolver.
- Keep current per-event projection as fallback.
