# Local Multicast Testing Mode (Single-Instance, Large-N)

## Summary

This document describes a **test-only** delivery mode that replaces pairwise sync
with a local multicast bus. All peers on the same instance receive the same
canonical events from a shared pool, but each peer still **records, decrypts,
and projects** independently. The goal is to scale to 10k+ peers on one machine
without per-connection sync traffic.

This mode is **not** meant to model real network behavior or protocol
reconciliation. It is a performance and scalability harness for projection,
encryption, and backlog handling.

---

## Goals

- Eliminate per-connection sync (negentropy or windowed sync) in tests.
- Deliver canonical events once, then fan-out to many peers.
- Preserve per-peer decryption and projection cost.
- Support partition testing via delivery filters and catch-up replay.
- Keep production code paths unchanged (test-only).

## Non-Goals

- Modeling real packet loss, latency, or NAT behavior.
- Testing sync protocol correctness or negotiation efficiency.
- Preserving realistic network ordering across connections.

---

## Core Concept

**Canonical Event Pool**
- All new shareable events are stored once in a single canonical pool
  (content-addressed blob).

**Per-Peer Fan-Out**
- For each local peer in the same community, create a recorded entry referencing
  that canonical event blob.
- Each peer decrypts and projects independently.

This preserves the per-peer cost but removes per-connection sync and transit
overhead.

---

## Partition Testing Model

Partitions are modeled at the **delivery layer**, not the sync layer.

### Option A: Partition Groups

- Each peer has a `partition_id`.
- Fan-out only to peers in the same partition.
- When partitions heal, move peers to the same partition and **replay** missed
  events from the canonical pool.

**Replay approach:**
- Track `last_seen_seq` per peer (or per partition).
- On heal: deliver events with seq > last_seen_seq.

### Option B: Delivery Filter + Cursor

- Maintain a global sequence for canonical events (monotonic integer).
- Each peer has a `last_seen_seq`.
- A partition is simply a filter that pauses delivery to a peer.
- Healing resumes delivery from the last seen sequence.

---

## Proposed Test-Mode API (Conceptual)

The test harness should expose a small API surface to avoid touching production
code paths.

Suggested functions:

- `multicast_enable(enabled: bool)`
- `multicast_set_partition(peer_id: str, partition_id: str)`
- `multicast_heal(partition_a: str, partition_b: str)`
- `multicast_replay(peer_id: str, from_seq: int)`
- `multicast_stats() -> dict`

The implementation can be kept entirely in test scaffolding or simulator code.

---

## Delivery Flow (Test Mode)

1. A new **shareable** event is created locally.
2. The event blob is stored once in the canonical pool.
3. A global `seq` is assigned (monotonic).
4. For each eligible peer:
   - If peer is in the same community and not partitioned, create a recorded
     entry pointing at the event blob (do not duplicate the blob).
   - Update peer cursor `last_seen_seq = seq`.

If a peer is partitioned, steps 4 are skipped. When the partition heals, replay
any events with `seq > last_seen_seq`.

---

## Invariants to Preserve

- **Per-peer projection remains unchanged.**
- **Canonical blobs are immutable and stored once.**
- **Only shareable events are fanned out.**
- **Peers only receive events for their community/network.**
- **No sync protocol messages are generated in this mode.**

---

## What This Tests Well

- Decryption and projection throughput at large N.
- Memory/DB overhead of per-peer recorded + projection tables.
- Backlog handling on partition heal.
- TreeKEM-style key distribution cost (if implemented in events).

## What This Does NOT Test

- Sync protocol correctness or performance.
- Loss recovery behavior or network ordering.
- Cross-instance delivery and NAT traversal.

---

## Example Test Scenarios

1. **Large Fan-Out**
   - 100k peers in one community.
   - Create 1,000 shareable events.
   - Measure total time and per-peer projection latency.

2. **Partition Split/Heal**
   - Split peers into A and B partitions.
   - Generate events in both partitions.
   - Heal and replay; verify convergence and projection correctness.

3. **TreeKEM Update Cost**
   - Trigger N updates with a large membership set.
   - Measure total crypto time and payload sizes.

---

## Notes on "Transport-Like" Integration

If desired, this can be exposed as a **test-only transport mode**:

- The "send" operation directly inserts the canonical blob into the multicast
  pool and triggers fan-out.
- The "receive" operation is bypassed (events are already recorded).

If transit wrapping is required for compatibility with existing pipelines,
consider using a **shared local bus key** so the transit unwrap can happen once,
then fan-out the resulting plaintext event blob.
This is still test-only and should not affect production behavior.
