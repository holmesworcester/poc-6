# TLA+ Bootstrap Graph Model

This folder contains the TLA+ model for the ideal, trust-anchored bootstrap
and the connection upgrade chain. The model is intended as a small, checkable
spec for projector causality and bootstrap ordering.

Files
- `docs/tla/BootstrapGraph.tla`: model of the identity chain and connection
  bootstrap/upgrade invariants.
- `docs/tla/bootstrap_graph.cfg`: TLC config with invariants.
- `docs/tla/states/`: TLC output directory (generated; not tracked).

State summary (BootstrapGraph.tla)
- `recorded`: events with stored blobs/recorded wrappers.
- `valid`: events that have projected and are valid.
- `trustAnchor`: whether an invite_accepted has established a network anchor.
- `connReq`, `connAck`: connection request/ack progression.
- `connInvite`: invite-labeled connection active.
- `connPeer`: peer-labeled connection active.

Key invariants (see config)
- `InvNetAnchor`: network valid implies a trust anchor exists.
- `InvDeps`: all valid events have their dependencies satisfied.
- `InvConnReq`/`InvConnAck`/`InvConnInvite`/`InvConnPeer`: enforce bootstrap
  connection causality and upgrade ordering.

Running TLC
```bash
java -cp /tmp/tla2tools.jar tlc2.TLC \
  -config docs/tla/bootstrap_graph.cfg \
  docs/tla/BootstrapGraph.tla
```

The model is small enough for full state exploration.
