# CC Real-World Throughput Review

Summary: current CC will converge but real-world throughput will be modest, especially on WANs.

Key issues:
- Window cap (MAX_WINDOW=8) + 512B packets limits throughput (e.g., ~40KB/s at 100ms RTT).
- CC gates range_request count, not bulk bytes; event blobs bypass CC and can still burst.
- Sync loop only sends one root request per second; child range_requests are sent by responder without CC gating.
- in_flight accounting only decrements on range_matched (not range_events), so window can stall.
- Simulator tests don’t model bandwidth constraints, so throughput claims aren’t validated.

Implications:
- LAN/small history sync is fine; WAN or large histories will be slow.
- CC won’t prevent bursts during blob transfers, so loss can still be high.

Suggestions (if aiming for better throughput):
- Gate bytes (pacing) in addition to request count; apply to event blobs.
- Treat range_events as a response for in_flight accounting.
- Allow multiple requests per tick or reduce NegentropySyncJob interval.
- Consider BDP-based window growth rather than fixed MAX_WINDOW.
