# UDP Network Simulator (Compact Design)

## Overview
UDP-like simulator + address router + NAT model for realistic single-process testing. Supports per-peer address assignment, NAT roles, latency/loss, and encrypted flows so multiple local identities can join overlapping networks with correct visibility.

## Compact Plan
- Components:
  - Simulator: `core/network_simulator.py` (latency, loss, size limit)
- Core Network API: `core/net.py` (re-exports active backend)
  - Handlers: use existing `send_to_network`/`receive_from_network`
- Address router (core):
  - Assigns each peer a local endpoint `(local_ip, local_port)`; persists to `addresses`
  - Maps delivered `(dest_ip, dest_port)` → local `peer_id` as `to_peer`
- NAT simulation (core+sim):
  - Per-peer role: `public` or `behind_nat`
  - NAT config: mode (`full_cone`, `restricted`, `symmetric`), mapping TTL, port preservation, hairpinning
  - Outbound creates/refreshes NAT mapping; inbound allowed per NAT mode
  - Public endpoint discovery via “observed origin_ip/port” (STUN-like) from other peers
- Encryption gates visibility:
  - Event layer: per-network (later per-group) symmetric key distributed via invite
  - Transit: per-network secret initially; sync-request may seal to peer
  - Only decryptable events project/store; reads require membership

## Key APIs to Add (core/network.py)
- Address/NAT lifecycle:
  - `init_simulator(net_cfg: NetworkConfig, nat_cfg: NatConfig)`
  - `register_peer(peer_id: str, role: Literal['public','behind_nat'], local_ip: str | None = None) -> (local_ip, local_port)`
  - `claim_port(peer_id: str, preferred: int | None = None) -> int` (bind a local UDP port)
  - `get_public_endpoint(peer_id: str) -> (ip, port)` (via NAT mapping or public)
  - `router_resolve(dest_ip: str, dest_port: int) -> peer_id | None` (maps to local peer)
- Send/receive:
  - `send_raw(dest_ip, dest_port, raw_data, due_ms=None, origin_ip=None, origin_port=None)`
    - If origin unset, derive from `peer_id`’s bound port and local IP; NAT translates to public mapping
  - `deliver_due(current_time_ms=None) -> list[envelope]` (sim returns envelopes with `raw_data`, `origin_ip/port`, `dest_ip/port`, `received_at` and `received_by_*`)

Configuration control lives in `core/network.py` via `NatConfig` and per-peer roles at `register_peer` time; tests/demo can set these explicitly.

## Key Features
- Packet size limit, packet loss, latency (simulator config)
- Address router: `(dest_ip, dest_port)` → local `peer_id`
- NAT roles and behavior with mapping TTL, port preservation, hairpinning
- “Observed” public endpoint reporting for NAT discovery
- Deterministic time via `deliver_due(current_time_ms)`

## Architecture

### Core Components

#### 1. Network Simulator Module (`core/network_simulator.py`)
- `UDPNetworkSimulator` class managing packet routing and timing
- `NetworkConfig` dataclass for configuration
- `PendingPacket` dataclass for queued packets
- **Core-level `send()` and `receive()` functions** that can be called directly by handlers or tests
- `receive()` outputs envelopes that match `ReceiveFromNetworkHandler`'s filter (with `raw_data`, `origin_ip`, etc.)
- The simulator itself is protocol-agnostic and lives entirely in core

#### 2. Address Event Type (`protocols/quiet/events/address/`)
- Address event handling: handlers (reflectors/flows) emit add/remove/update events when endpoints are learned (e.g., from observed origin or intro messages).
- Validator for address format and uniqueness
- Projector maintains the `addresses` table
- Note: Routing uses both network/transit context and addressing; loopback on the same IP/port is supported.

#### 3. Core Network Functions (`core/network.py`)
- Simulator facade: `init_simulator`, `send_raw`, `deliver_due`
- Address/NAT APIs: register peers, bind ports, resolve receiver, report public endpoints

## Data Flow

### Sending Process
1. `SendToNetworkHandler` calls `net_send(peer_id, dest_ip, dest_port, raw_data, due_ms)` with:
   - `dest_ip` and `dest_port`: Destination address
   - `transit_ciphertext`: Encrypted payload with transit key ID prefix
   - `due_ms`: Optional send time

2. Network simulator:
   - Checks packet size (drops if > 600B)
   - Applies packet loss (random drop based on configured rate)
   - Calculates delivery time: `current_time + latency_ms`
   - Queues packet for delivery

### Receiving Process
1. Core calls `deliver_due(current_time_ms)`
2. Simulator returns envelopes with: `raw_data` (transit_secret_id + ciphertext), `origin_ip/port`, `dest_ip/port`, `received_at`, `received_by_*`
3. Address router maps `received_by_*` → `to_peer` (local), annotate envelope
4. `receive_from_network` extracts transit layer; rest of pipeline proceeds
5. Address changes are delivered at the app layer via intro/reflect flows (see Address Discovery & Intro); they are not special network envelopes.

## Envelope Fields

### Input to net_send (from SendToNetworkHandler)
```python
{
    'dest_ip': '192.168.1.101',
    'dest_port': 5000,
    'transit_ciphertext': b'...',  # Raw wire payload (transit_secret_id + ciphertext)
    'transit_secret_id': 'abc123...',  # For reference (may be omitted in future)
    'due_ms': 1234567890  # Optional
}
```

### Output from receive_packets (to ReceiveFromNetworkHandler)
```python
{
    'raw_data': b'...',  # 32-byte transit key ID + ciphertext
    'origin_ip': '192.168.1.100',
    'origin_port': 5000,
    'received_at': 1234567950  # Delivery timestamp
}
```

## Address Management

### Address Event Structure
```python
{
    'event_type': 'address',
    'action': 'add' | 'remove',
    'peer_id': 'peer_123',  # ID of the peer advertising this address
    'ip': '192.168.1.100',
    'port': 5000,
    'timestamp_ms': 1234567890
}
```

### Address Registration Flow
1. Peer creates address event with action='add' using their peer_id
2. Event is validated and projected to database
3. Addresses are used for routing decisions at application layer

### Simplified Packet Delivery
Simulator is a dumb pipe (size/loss/latency); address router/NAT apply app-layer routing rules. Transit/event crypto gates visibility: packets without correct keys are dropped at decrypt.

## Database Schema

### Address Table
```sql
CREATE TABLE IF NOT EXISTS addresses (
    peer_id TEXT NOT NULL,
    ip TEXT NOT NULL,
    port INTEGER NOT NULL,
    registered_at_ms INTEGER NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (peer_id, ip, port)
);
```

## Integration with Existing Handlers
- Send path: `check_outgoing` → `crypto` (event + transit) → `send_to_network` → `net_send(...)`
- Receive path: `net_deliver(...)` returns delivered network envelopes (with `to_peer`) → `receive_from_network` (transit) → `resolve_deps` → `crypto` (open/decrypt) → `validate` → `project` → `reflect`

## Core vs Handler Split (Recommended)

- Core (transport/NAT, protocol‑agnostic):
  - Simulator: latency, loss, MTU, deterministic time, queues
  - NAT engine: roles (public/behind_nat), modes (full_cone/restricted/symmetric), mapping TTL, hairpinning, port preservation
  - Address router: assigns local endpoints and maps `(dest_ip, dest_port)` to local `to_peer` (so handlers can’t bypass routing)
  - Peer endpoint lifecycle: assign local ports, manage NAT mappings, derive public endpoint
  - APIs only; no DB/protocol coupling.

- Handler (protocol‑aware only):
  - Use Core Network API (`net_*`) exclusively
  - Outgoing selects `address_id` via protocol state; Core handles origin/public mapping and delivery
  - Reads enforce membership (queries join `users`/`group_members`)
  - After network delivery, run transit/event crypto → validate → project → reflect

Rationale: Handlers cannot “cheat” the simulator; all network mechanics (routing/NAT/origin) live behind a stable Core Network API.

## Core Network API (Stable)

Defined by core and implemented by the active backend (start with simulator). Handlers import only these functions (e.g., `from core.net import net_send, net_deliver, ...`). Later, a real network backend can replace the simulator with the same signatures.

- `net_init(net_cfg: NetworkConfig, nat_cfg: NatConfig) -> None`
  - Initialize transport + NAT engine.
- `net_register_peer(peer_id: str, role: Literal['public','behind_nat']) -> tuple[local_ip: str, local_port: int]`
  - Assign a local endpoint and register NAT role.
- `net_claim_port(peer_id: str, preferred: int | None = None) -> int`
  - Bind a local UDP port for the peer.
- `net_public_endpoint(peer_id: str) -> tuple[ip: str, port: int] | None`
  - Return best-known public mapping if available (None if unknown).
- `net_status(peer_id: str) -> dict`
  - Returns core’s view: `{ role, public_endpoint, last_mapping_ms, online_guess }`.
- `net_poll_events() -> list[dict]`
  - Optional: returns core-generated status events like `address_changed`, `mapping_expired`. Caller can feed into pipeline or handle directly.
- `net_send(peer_id: str, dest_ip: str, dest_port: int, raw_data: bytes, due_ms: int | None = None) -> bool`
  - Send raw wire bytes (transit_key_id + ciphertext). Core chooses origin endpoint and applies NAT.
- `net_deliver(current_time_ms: int | None = None) -> list[dict]`
  - Return delivered packets as network envelopes: `raw_data`, `origin_ip/port`, `dest_ip/port`, `received_at`, and `to_peer` (resolved by core router).
  - Consumption model: the caller (e.g., `API.tick_network()` or tests/demo) feeds these envelopes into the pipeline as `input_envelopes`. The existing `receive_from_network` handler registers via its `filter(...)` (matches `raw_data`, `origin_ip`, `origin_port`, `received_at` fields) and processes them into transit-layer envelopes.

Implementation now: `core/network_simulator.py` provides these functions. A tiny alias module `core/net.py` can re-export the active backend so handler imports stay stable. Later, a real network backend will implement the same API and can be switched in.

## Time Management

### Simulation Modes

#### 1. Real-time Mode (Production)
- Uses actual system time
- Delivers packets based on wall clock

#### 2. Simulated Time Mode (Testing)
- Controlled time advancement
- Deterministic packet delivery
- Useful for integration tests

Connectivity heuristics:
- `online_guess` becomes true when:
  - a packet is delivered to or from `peer_id` within a sliding window, or
  - behind NAT: a fresh mapping exists (TTL not expired) and at least one successful send since last mapping
- It becomes false when:
  - no deliveries within the window AND mapping expired (for NAT), or
  - explicit local disconnect (e.g., address/port released)
- Core emits `connectivity_changed` via `net_poll_events()` when this flips.

### Time Advancement
```python
# Advance time by 100ms and get delivered packets
delivered = simulator.advance_time(100)

# Or check for packets at specific time
delivered = simulator.receive_packets(current_time_ms=1234567890)
```

## NAT Model (Concise)
- Per-peer role: `public` or `behind_nat`
- Modes: `full_cone`, `restricted`, `symmetric`
- Mapping: (local_ip, local_port, remote_tuple) → (public_ip, public_port)
  - `full_cone`: one public mapping per local port; any remote may reply
  - `restricted`: mapping plus remote IP check
  - `symmetric`: unique mapping per remote (IP, port)
- TTL: mappings expire after `mapping_ttl_ms`
- Port preservation: try to reuse local port externally (probabilistic)
- Hairpinning: allow internal peers to send to their own public mapping if enabled
- Public endpoint discovery: peers learn their `(public_ip, public_port)` from another peer’s `origin_ip/port` on received packets and can update `addresses`

## Testing Strategy (Concise)

### Unit Tests
1. Packet size enforcement
2. Packet loss application
3. Latency calculation
4. Address registration/lookup and router resolution
5. NAT mapping creation/expiry; mode behaviors
6. Time advancement

### Integration Tests
1. Overlapping networks on single client (A in Net1+Net2; B in Net1; C in Net2)
2. Sync-request + responses over simulator
3. Address changes (rebind/port change) and NAT remapping
4. Optional: packet loss/jitter variations

## Single-Client, Multi-Network Simulation (Encryption + NAT)

Goals:
- Multiple local peers in overlapping networks; correct visibility/encryption
- Realistic routing via address router + NAT roles

Plan:
1) Setup
   - `init_simulator(NetworkConfig, NatConfig)`; register A/B/C with roles
   - `claim_port` for each; record local addresses; optionally pre-populate public mappings
   - Invites carry per-network symmetric key and (optionally) transit secret
   - Create channels for Net1/Net2
2) Send (per message):
   - `resolve_deps` resolves `address_id` → dest endpoint
   - `crypto` event-layer encryption (network key) or seal-to-peer
   - `crypto` transit encryption (network transit or per-peer)
   - `send_to_network` → `send_raw` (NAT translates origin)
3) Deliver:
   - `deliver_due` returns packets with origin/public mapping applied
   - Address router maps `received_by_*` → `to_peer`
   - `receive_from_network` + `resolve_deps` + `crypto` decrypt/open
   - `validate` + `project` store
4) Assert visibility:
   - A (Net1+Net2) sees Net1+Net2 messages
   - B (Net1) sees Net1 only
   - C (Net2) sees Net2 only

Notes:
- Reads enforce membership (queries join `users`/`group_members`)
- Public endpoint discovery: peers can learn/update their public `(ip, port)` by reading `origin_ip/port` from received packets and projecting address updates

## Address Discovery & Intro (Practical Flow)

- Goal: Peers behind NAT don’t know their public endpoint; they learn it from others observing `origin_ip/port` and sending that information back (intro/reflect).

- Control points:
  - NAT role set at `net_register_peer(peer_id, role=...)`.
  - NAT behavior set at `net_init(..., NatConfig)`: mode, TTL, hairpinning, port preservation.
  - A peer’s `net_public_endpoint(peer_id)` is a best-effort observation (may be None). Protocol remains authoritative via intro/address events; core does not write protocol state.

- Discovery steps (one option):
  1) Bootstrap/Invite: Peer A (public) invites Peer B (behind_nat) including A’s reachable endpoint. 
  2) First contact: B sends a packet to A. NAT creates mapping; simulator sets `origin_ip/port` on the packet as B’s public mapping.
  3) Observation: A receives the packet; handler reads `origin_ip/port` and emits an intro/address-update event. Simpler starting point: A broadcasts an intro event with B’s observed endpoint to the network (or targets B directly); we can refine scope/privacy later.
  4) Learn: B receives the response; handler projects its new public `(ip, port)` and can publish/update its address record.
  5) Keepalive: B periodically sends keepalives to maintain mapping; if TTL expires, mapping is lost until another outbound packet recreates it.

  Self-discovery: A learns its own public endpoint from intro/address-update events emitted by others that observed A’s `origin_ip/port`. Data flow: net_deliver → receive_from_network → decrypt → validate → project (address update for A). A can also query `net_status(A)` for best-effort observations, but protocol-level intro remains authoritative.

- Variants:
  - Symmetric NAT: mapping is per-destination, so B’s public `(ip, port)` toward A may differ when talking to C.
  - Hairpinning: If enabled, peers behind the same NAT can reach each other via public endpoints; otherwise, require local endpoints.

- Handler responsibilities in this flow:
  - Read `origin_ip/port` from delivered envelopes to detect remote public endpoints.
  - Emit intro/address-update events to inform remote peers of their observed public endpoint (sealed so only they can read it).
  - Update local address state (projector) when we learn our own or a remote’s public endpoint.

- Core responsibilities:
  - Maintain NAT mappings and apply them to outbound/inbound packets.
  - Include `origin_ip/port` and a best-effort `to_peer` hint in delivered envelopes so handlers can discover/update addresses without touching transport internals. NetworkGate later attaches the authoritative receiver identity (`to_peer` as peer_secret_id and `to_peer_id` as peer_id) from transit/prekey deps.

### Envelope Gating and Decryption Rules

1) Receive routing (address → peer):
- The simulator’s delivered envelope includes `dest_ip`/`dest_port` (and we augment `received_by_ip`/`received_by_port`).
- A small “address router” step (in the simulator) annotates the envelope with a best‑effort `to_peer` hint. NetworkGate then derives the authoritative receiver identity from transit/prekey deps and sets `to_peer` (peer_secret_id) and `to_peer_id` (peer_id).
- Only envelopes that resolve to a known local `peer_id` proceed.

2) Transit decrypt:
- `receive_from_network` extracts `transit_secret_id` and `transit_ciphertext` from `raw_data`.
- `resolve_deps` supplies the transit secret material based on `transit_secret_id`. Two near-term modes:
  - per-network transit secret: `transit_secret_id` corresponds to a network-scoped secret shared by members (simplest to start; secret arrives via invite flows and is recorded in `peer_transit_keys`).
  - sealed-to-peer: for sync-request, the event_plaintext was sealed to `to_peer`’s public key; transit decrypt reveals a sealed event that only `to_peer` can open.
- `crypto` performs transit decrypt; if the key isn’t present for this `to_peer`/network, decryption fails and the envelope is dropped.

3) Event-layer decryption/sealing:
- Symmetric (per-network or per-group): After transit decrypt, `key_ref` indicates a symmetric key. `resolve_deps` provides that key only if `to_peer`’s identity is a member of that network/group (learned via invite → user → group membership). If missing, decryption fails and no projection occurs.
- Sealed-to-peer (KEM-style): If `key_ref.kind == 'peer'`, the decrypted payload is sealed to the receiving peer’s public key; only that peer can open it. Others ignore it.

4) Projection and storage:
- Only successfully decrypted and validated events set `write_to_store=True` and are projected into tables (e.g., `messages`). This prevents “global visibility” in a single DB: packets intended for other peers won’t decrypt under the wrong `to_peer` context and thus will not be projected by that local pass.

5) Query-time access control:
- Strengthen `message.get` to enforce membership by joining `users`/`group_members` tables so that an identity not in a network/group cannot read messages even if present in the DB (e.g., imported by another local peer). This makes tests robust and matches intended semantics.

### Simulator-Driven Test Scenario (Overlapping Networks)

Setup:
- Identities: Alice (A), Bob (B), Charlie (C).
- Networks: Net1, Net2.
  - Alice joins Net1 and Net2.
  - Bob joins Net1 only.
  - Charlie joins Net2 only.
- Each network has:
  - A symmetric event key (initially one key per network, included in the invite link and projected as a `key` event; later: per-group keys sealed to members and rotated).
  - A transit key context: start with a simple per-network transit secret (shared via `transit_secret` event and invite). Later, move to ephemeral transit keys derived per-session.
- Addresses: Register a distinct `(ip, port)` for each local peer (A, B, C) in the `addresses` table.

Flow:
1. Initialization
   - Call `core.network.init_simulator()` with `latency_ms` (e.g., 25ms) and `loss=0`.
   - Use flows (`user.join_as_user`) to create users for A/B/C in their respective networks using invites that carry:
     - network_id, group_id
     - per-network symmetric key (for event-layer)
     - (optionally) transit key id or a seed to derive it
   - Create channels for Net1 and Net2 (e.g., `general1`, `general2`).

2. Sending messages
   - Alice sends m1 in Net1 → envelope marked `is_outgoing`, with `address_id` targeting B (and also A for her own device if echoing), `event_plaintext` includes `network_id` and `channel_id` for Net1.
   - Alice sends m2 in Net2 → address/target is C (and A if echoing) with Net2 context.
   - Bob sends m3 in Net1 → address/target is A (and B if echoing), Net1 context.
   - Charlie sends m4 in Net2 → address/target is A (and C if echoing), Net2 context.
   - Outgoing pipeline for each message:
     - `resolve_deps` resolves `address_id` → concrete dest IP/port.
     - `check_outgoing` sets `outgoing_checked` and copies `dest_ip/port`.
     - `crypto` event-layer encryption: symmetric per-network key (or sealing if peer-targeted).
     - `crypto` transit encryption: per-network transit key (stub) or per-peer transit context.
     - `send_to_network` → `core.network.send_raw` → simulator queue.

3. Delivery and processing
   - Call `core.network.deliver_due()` (via future `API.tick_network()`), which returns delivered packets with `origin_ip/port`, `dest_ip/port`.
   - For each delivered packet:
     - Address router maps `dest_ip/port` → `to_peer`.
     - `receive_from_network` extracts transit layer.
     - `resolve_deps` provides transit key for the network of `to_peer` or fails.
     - `crypto` transit decrypt. If it yields `key_ref`:
       - If `key_ref.kind == 'key'`, `resolve_deps` supplies the symmetric key only if `to_peer` belongs to that network/group; then event decrypt succeeds.
       - If `key_ref.kind == 'peer'`, unseal only if the packet is sealed to `to_peer`.
     - `validate` + `project` store the message only when decryption succeeded.

4. Assertions
   - After a few ticks (scheduler + network), query `message.get` as each identity (with improved membership checks):
     - Alice sees m1 (Net1), m2 (Net2), m3 (Net1 from Bob), m4 (Net2 from Charlie).
     - Bob sees m1 (Net1), m3 (Net1), but not m2 or m4 (Net2 only).
     - Charlie sees m2 (Net2), m4 (Net2), but not m1 or m3 (Net1 only).

### Practical Notes for POC

- Start simple:
  - Per-network symmetric key for event-layer encryption, included in invite link; projector stores the `key` event and `resolve_deps` exposes the unsealed secret to crypto.
  - Per-network transit key (via `transit_secret` event) to enable transit decrypt. Later, move to per-peer transit with ephemeral keys for stronger privacy.
- Strengthen `message.get` to enforce membership (use `users` and `group_members` joins) so an identity outside a network can’t read messages even if another local identity stored them.
- Use the addresses table to route packets to specific local peers in a single process; only the targeted peer processes/decrypts.
- Keep loss=0 for deterministic tests; add loss later for robustness tests.

This setup allows exercising real network separation semantics (by keys and membership) while staying in a single process with a realistic send/receive loop via the UDP simulator.


### Test Utilities
```python
def create_test_network(peers: int, loss_rate: float = 0.0) -> UDPNetworkSimulator:
    """Create a test network with registered peers."""

def simulate_partition(simulator: UDPNetworkSimulator, group1: List[str], group2: List[str]):
    """Simulate network partition between two groups."""

def measure_delivery_rate(simulator: UDPNetworkSimulator, packets: int) -> float:
    """Measure actual packet delivery rate."""
```

## Implementation Phases

### Phase 1: Core Simulator and Functions
- [x] Basic simulator class in `core/network_simulator.py`
- [ ] Core network functions in `core/network.py`
- [ ] Packet queuing and delivery
- [ ] Size and loss simulation
- [ ] Time management

### Phase 2: Address Management
- [ ] Address event type in `protocols/quiet/events/address/`
- [ ] Address validation and projection
- [ ] Database schema for addresses table
- [ ] Core functions query DB for address lookups

### Phase 3: Handler Updates
- [ ] Update existing send/receive handlers to use core functions
- [ ] Pipeline integration
- [ ] Send/receive envelope conversion
- [ ] Handler tests

### Phase 4: Advanced Features
- [ ] Bandwidth limiting
- [ ] Packet reordering
- [ ] Jitter simulation
- [ ] Network topology (routing tables)

## Future Enhancements

1. **Bandwidth Limiting**: Enforce maximum throughput
2. **Packet Reordering**: Simulate out-of-order delivery
3. **Jitter**: Variable latency within ranges
4. **Network Topology**: Define network graphs with routing
5. **Congestion Simulation**: Dynamic loss based on traffic
6. **MTU Fragmentation**: Handle larger packets with fragmentation
7. **NAT Simulation**: Address translation and traversal

## Example Usage

```python
from core.network_simulator import UDPNetworkSimulator, NetworkConfig
from core.network import send_packet, receive_packets

# Create simulator with realistic conditions
simulator = UDPNetworkSimulator(NetworkConfig(
    packet_loss_rate=0.02,
    latency_ms=30,
    max_packet_size=600
))

# In practice, addresses are registered via address events in the database
# The simulator queries the DB, it doesn't store addresses itself

# Send a packet using core function
send_envelope = Envelope({
    'is_send': True,
    'origin_ip': '192.168.1.100',
    'send_to': [('192.168.1.101', 5000)],
    'sent_at_ms': 1000,
    'event_blob': b'\x01' + key_id_bytes + nonce24 + ciphertext,
    'event_type': 'message'
})

# Core function handles DB lookup and simulator interaction
immediate = send_packet(db, simulator, send_envelope)

# Advance time and receive packets
current_time_ms = 1050  # 50ms later
received = receive_packets(db, simulator, current_time_ms)
```
