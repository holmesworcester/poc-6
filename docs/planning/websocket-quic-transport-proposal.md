# WebSocket/QUIC Transport Proposal

## Goal
Allow peers to sync using conventional HTTPS-style transport (WSS/HTTP3) for
NAT and firewall friendly connectivity.

## Non-goals
- Replace the existing UDP plan.
- Change sync protocol or event formats.
- Require server-side decryption or event parsing.

## High-level options
1. Direct peer listener over WSS/QUIC (when reachable).
2. Relay server where both peers connect outbound and the relay forwards opaque
   blobs.

## Transport abstraction (shared by WSS and QUIC)
Define a small transport interface so the sync layer does not care how packets
arrive. The receiver runs in a background thread and only pushes blobs into a
thread-safe queue. The main thread inserts into `incoming_blobs` and calls
`tick()` just like UDP.

```
class Transport:
    def start(self) -> None: ...
    def send(self, address: "PeerAddress", blob: bytes) -> None: ...
    def drain(self) -> list[bytes]: ...
```

This keeps SQLite access on the main thread and reuses the existing
`incoming_blobs` flow.

## Wire framing
Use binary frames with a tiny header. Handshake messages can be JSON, data
frames carry raw bytes.

Frame header:
- version (u8)
- type (u8): HELLO, HELLO_ACK, DATA, PING, PONG, CLOSE
- length (u32)
- payload (bytes)

HELLO payload (JSON):
```
{
  "peer_id": "...",
  "network_id": "...",
  "capabilities": ["wss", "quic", "relay"],
  "auth": { "nonce": "...", "signature": "..." }
}
```

HELLO_ACK payload (JSON):
```
{
  "session_id": "...",
  "heartbeat_ms": 10000,
  "server_time_ms": 1234567890
}
```

DATA payload:
- metadata (small JSON or msgpack):
  - `to_peer_id` (required for relay)
  - `from_peer_id`
  - `network_id` (optional)
- raw transit blob (opaque bytes)

For WSS, send a single binary frame that concatenates metadata + blob. For QUIC,
use a length-prefixed stream or datagram (if supported) with the same payload
format.

## Relay server
The relay is a thin router. It never decrypts or parses events.

Responsibilities:
- Map `peer_id -> connection(s)` on HELLO.
- Verify HELLO with a signed challenge to prevent spoofed peer_id.
- On DATA, forward payload to the `to_peer_id` connection(s).
- Optional store-and-forward with short TTL for offline peers.
- Rate limits per connection to protect the service.

## Helper server onboarding via invite links
We already have server onboarding language in:
- `docs/quiet-protocol-specification.md` (Optional Servers)
- `docs/planning/network-context-and-server-mode.md` (Server Invite Flow)

The helper server should be joinable with a normal invite link, but without
granting access to message groups. This keeps message content private while
allowing the server to relay opaque blobs.

Minimal flow:
1. Admin creates a normal invite link for a server (no group keys shared).
2. Client submits the invite link to the server's public onboarding endpoint.
3. Server accepts the invite, joins the network, and publishes its
   `peer_address` for relay traffic.

Example server endpoints (separate ports are fine):
- Relay traffic: `wss://relay.example/quiet` or `quic://relay.example/quiet`
- Invite onboarding: `https://relay.example:8443/submit-invite`

Onboarding request:
```
POST /submit-invite
{ "invite_link": "quiet://..."} 
```

Privacy note: the server should not be added to any message groups by default.
If a community wants the server to read messages, that should be explicit (e.g.
a "member" role or group key distribution change).

## Direct peer server
Each peer can run a small WSS/QUIC listener:

```
wss://host:port/quiet
quic://host:port/quiet
```

Invite links or `peer_address` events include the URL so the other peer can
connect directly when reachable. Once connected, the transport is treated the
same as UDP: just a way to deliver transit blobs into the queue.

## Address distribution
Extend `peer_address` events to carry conventional transport info:

```
{
  "transport": "wss" | "quic" | "relay-wss" | "relay-quic",
  "url": "wss://relay.example/quiet",
  "peer_id": "...",
  "network_id": "..."  # optional
}
```

Clients prefer direct addresses when reachable, fall back to relay addresses
otherwise.

## Security
- Payloads are already encrypted and signed at the event layer.
- HELLO uses challenge-response signatures to bind `peer_id` to the connection.
- Relay visibility is limited to metadata needed for routing.

## Integration with existing sync
No sync protocol changes:
- Send path: `sync.send_*` -> `transport.send(address, blob)`
- Receive path: transport recv thread -> queue -> `incoming_blobs` -> `tick()`

## Transport vs simulation modes (orthogonal)
Transport describes *how* blobs move between peers. Simulation describes *when*
they are delivered. These are independent axes:

Transport choices:
- UDP
- WSS/QUIC direct
- WSS/QUIC relay

Simulation choices:
- Loopback (same process, immediate delivery)
- Simulator (latency/loss/partition models)
- Real network (actual sockets)

All transports ultimately enqueue into `incoming_blobs`, and the simulator only
controls `deliver_at` timing or drop behavior. This keeps transports and
simulation orthogonal.

## CLI flow (native commands, scenario-test-first)
Add a CLI helper that composes existing commands instead of shelling out:

```
helper-add --endpoint https://relay.example:8443/submit-invite
```

Expected behavior:
1. Run `invite` internally to generate a normal invite link.
2. POST the invite link to the helper server's submit endpoint.
3. Record the helper's address (from response or follow-on `peer_address`).

No external tools (e.g. curl) required; use stdlib HTTP client.

Testing should start with scenario tests that:
- Create a network and an invite.
- Spin up a local fake submit endpoint.
- Call `helper-add` and assert the invite is posted and recorded.

## Implementation steps
1. Add `core/transport.py` interface and registry.
2. Implement WSS client/server with a background asyncio loop that drains into a
   thread-safe queue.
3. Implement QUIC client/server using `aioquic` with the same queue handoff.
4. Implement a small relay server (standalone tool) for routing.
5. Add CLI flags: `--wss`, `--quic`, `--relay`, plus `listen` helpers.

## Open questions
- Route by `peer_id` or subscribe by `transit_key_id` for finer routing?
- Multiple devices per peer_id: fan-out or unique device ids?
- QUIC datagrams vs streams for simplicity and MTU behavior?
