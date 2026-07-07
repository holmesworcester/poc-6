Adaptive Depth Feedback (Current)

Low-hanging fruit

- Batch send + bulk blob fetch: `_send_event_blobs()` still does per-event `get_shareable_blob()` and per-event `send()`. Add a bulk fetch (`SELECT blob FROM store WHERE id IN (...)`) and a `send_batch()` path to cut DB + connection lookups.
- Cap range explosion explicitly: even with `EVENTS_THRESHOLD=100`, mid-sized buckets can still split too far. Add a max-ranges guard or page inside a bucket instead of going to `prefix_10`.
- Remove per-message logging in `negentropy.handle_incoming()` to avoid per-packet overhead.
