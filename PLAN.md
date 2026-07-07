Negentropy Adaptive Splitting Plan (and Critique)

Goal
Make sync throughput scale with bytes, not range count, while keeping time-locality
and avoiding “bucket explosion” for mid-sized files.

Plan (Hybrid)
1) Keep deterministic time buckets as top-level ranges:
   - root -> prefix_2 -> prefix_4 -> prefix_6
   - This preserves fossilization and locality for old events.

2) Within a time bucket, switch to requester-driven splitting:
   - Requester chooses split bounds for the sub-range.
   - Responder only computes fingerprint for the requested bounds.
   - Prevents mismatched splits and keeps progress deterministic.

3) Split decision (requester side):
   - If count <= TARGET (e.g., 500–1000), stop splitting and send all blobs.
   - Else, split within the time bucket by:
     a) Histogram split using prefix counts (fast, O(256)).
     b) Or median-by-offset on unified_key for tighter balance.
   - Optionally page within a bucket instead of deeper split (after_key + limit).

4) Cap range explosion:
   - Stop splitting if expected ranges would exceed MAX_RANGES.
   - Use paging inside a bucket when MAX_RANGES would be exceeded.

Critique / Risks
- Protocol complexity:
  Requester-driven bounds require explicit lo/hi (or prefix+after) in the wire format.
  This is a protocol change; mixed-version peers won’t interop.

- Counting cost:
  Median-by-offset uses LIMIT/OFFSET which can be O(n) per split without
  additional indexing tricks. Histogram split is faster but coarser.

- Paging tradeoff:
  Paging avoids range explosion but increases per-bucket round trips.
  Needs careful time budgets to avoid slowing small buckets.

- State tracking:
  Requester must track its own split ranges; responder stays simple.
  This is fine but adds bookkeeping to the requester.

Why this is better than more levels
- Extra fixed prefix levels only add 16x jumps and move the cliff.
- Adaptive split + paging keeps range count bounded and performance smooth.
