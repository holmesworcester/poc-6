# QUIC Hole Punching Notes

This doc summarizes a practical QUIC hole-punching approach for peer-to-peer sync and how it maps onto a single-socket QUIC design. It also records key findings from recent QUIC NAT traversal work.

## Summary

- Use a rendezvous/relay to exchange public endpoints, then have both peers send packets to each other (classic simultaneous open). [1]
- Prefer a single QUIC connection per peer pair; use QUIC streams for multiplexing app traffic.
- Run QUIC on one UDP socket/port and demultiplex by Connection ID (CID); avoid zero-length CIDs if sharing a port across multiple connections. [3]
- Expect some NATs to fail; keep a relay fallback. [1]
- QUIC-based hole punching reduces time vs TCP-based approaches, and connection migration is cheaper than re-punching after path loss. [2]

## Background: Hole Punching Basics

Hole punching works by creating NAT bindings on both sides so packets from the peer are accepted. A typical flow:

1. Both peers learn their public (reflexive) address via a rendezvous server.
2. The server exchanges the public endpoints between peers.
3. Both peers send packets to each other, creating NAT bindings.
4. Once packets get through, they can establish a direct connection.

This is the standard NAT traversal pattern used by ICE and is described in QUIC-centric terms in Seemann/Huitema’s P2P QUIC overview. [1]

## QUIC-Specific Mechanics

QUIC already has path probing and migration, which look similar to punching: a path challenge/response validates a path and can be used to establish NAT mappings if both sides send probe packets. That makes QUIC a natural fit for hole punching workflows. [1]

The QUIC NAT hole punching paper reports:

- QUIC-based hole punching reduces punch time vs TCP-based schemes, especially in weaker network conditions.
- When a punched path breaks (e.g., NAT timeout or interface change), QUIC connection migration saves 2 RTTs vs QUIC re-punching and 3 RTTs vs TCP re-punching. [2]

These results suggest a design where direct QUIC connections are the first choice, but recovery prefers migration when possible.

## Single Socket / Many Connections

QUIC uses Connection IDs to match incoming packets to connections independent of 5-tuple changes. RFC 9000 warns that multiplexing multiple connections on the same local IP/port with **zero-length** CIDs is unsafe; use non-zero-length CIDs and supply peers with a CID pool instead. [3]

Practical implications:

- **Use one UDP socket** (bound once) for all QUIC connections and hole-punch traffic.
- **Ensure non-zero-length CIDs** so incoming packets can be demultiplexed correctly. [3]
- **Accept incoming connections** on the same socket; unknown DCIDs can create new connections on the server side. [3]

## Suggested Flow (P2P)

1. **Rendezvous**: each peer registers with a relay, which shares public endpoints.
2. **Simultaneous open**: both peers send QUIC Initials (or probe packets) toward each other’s public endpoints.
3. **Accept + tie-break**: if both sides dial, keep one connection (e.g., deterministic peer ID ordering) and close the other.
4. **Steady state**: single QUIC connection per peer, multiple streams for sync / control / data.
5. **Fallback**: if hole punching fails, stay on relay.

## Failure Modes & Mitigations

- **Symmetric or highly restrictive NATs**: punch may fail; relay required. [1]
- **NAT timeouts**: keep-alives or rely on QUIC migration (cheaper than re-punch). [2]
- **CID exhaustion**: keep a CID pool per peer and rotate; do not use zero-length CIDs on shared ports. [3]

## References

1. Marten Seemann, Christian Huitema. "A p2p Vision for QUIC" (Oct 26, 2024). https://seemann.io/posts/2024-10-26---p2p-quic/
2. Jinyu Liang, Wei Xu, Taotao Wang, Qing Yang, Shengli Zhang. "Implementing NAT Hole Punching with QUIC" (arXiv:2408.01791). https://arxiv.org/abs/2408.01791
3. RFC 9000: "QUIC: A UDP-Based Multiplexed and Secure Transport" (May 2021). https://quicwg.org/base-drafts/rfc9000.html
