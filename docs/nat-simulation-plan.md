# NAT Simulation Implementation Plan

## Overview

Add realistic NAT simulation to test hole punching behavior. Peers can be put
behind NAT via CLI, and the simulator enforces real NAT semantics for packet
routing.

## Current State

**What exists:**
- `simulator/nat.py`: NatEngine with modes (full_cone, restricted, symmetric),
  mapping TTL, but no actual packet filtering enforcement
- `events/network/intro.py`: Create/project intro events for hole punch coordination
- `test_nat_hole_punch.py`: Basic test that verifies intro events but doesn't
  actually test NAT packet filtering

**What's missing:**
- CLI command to put peers behind NAT
- Actual NAT enforcement in packet routing
- Hole punch mechanics (simultaneous packet exchange)
- Keepalive/mapping refresh
- Job to process intro events and trigger hole punching

## Implementation Plan

### Phase 1: NAT State Management

**1.1 Extend network_config.py with per-peer NAT state**

```python
@dataclass
class PeerNatConfig:
    """NAT configuration for a specific peer."""
    behind_nat: bool = False
    nat_mode: str = 'port_restricted'  # 'full_cone', 'restricted', 'port_restricted', 'symmetric'
    mapping_ttl_ms: int = 120_000  # 2 minutes (realistic for strict NATs)

# Global peer NAT configs
_peer_nat_configs: Dict[str, PeerNatConfig] = {}

def set_peer_nat(peer_id: str, behind_nat: bool, nat_mode: str = 'port_restricted'):
    """Put a peer behind NAT or remove from NAT."""

def get_peer_nat(peer_id: str) -> Optional[PeerNatConfig]:
    """Get NAT config for a peer."""

def is_behind_nat(peer_id: str) -> bool:
    """Check if peer is behind NAT."""
```

**1.2 Add CLI commands**

```
nat <n>                     Put account #n behind NAT (port-restricted, punchable)
nat <n> --mode <mode>       Put behind NAT with specific mode
nat <n> --off               Remove from NAT (direct connectivity)
nat-status                  Show NAT status for all peers
```

### Phase 2: NAT Enforcement in Packet Routing

**2.1 Enhance queues.py to enforce NAT**

The `incoming.add()` function needs to:
1. Check if destination is behind NAT
2. If yes, verify there's a valid inbound mapping (hole was punched)
3. If no valid mapping, drop the packet
4. Track outbound packets to create mappings

```python
def add(blob, t_ms, unsafedb, from_peer=None, to_peer=None):
    # ... existing checks ...

    # NAT check: if to_peer is behind NAT, verify hole is punched
    if to_peer and network_config.is_behind_nat(to_peer):
        if not nat_engine.has_inbound_mapping(to_peer, from_peer):
            log.debug(f"dropping packet: {to_peer} behind NAT, no hole punched from {from_peer}")
            return False

    # If from_peer is behind NAT, create/refresh outbound mapping
    if from_peer and network_config.is_behind_nat(from_peer):
        nat_engine.create_or_refresh_mapping(from_peer, to_peer, t_ms)
```

**2.2 Global NAT engine instance**

```python
# network_config.py
_nat_engine: Optional[NatEngine] = None

def get_nat_engine() -> NatEngine:
    """Get global NAT engine instance."""
```

### Phase 3: Hole Punch Mechanics

**3.1 Add IntroProcessJob to jobs.py**

```python
class IntroProcessJob(Job):
    """Process pending intro events and trigger hole punching."""

    def __init__(self):
        super().__init__('intro_process', every_ms=500)  # Check twice per second

    def run(self, t_ms: int, db: Any) -> dict:
        from events.network import intro

        # For each local peer
        for peer in get_local_peers(db):
            # Get pending intros where this peer is involved
            pending = intro.get_pending_intros(peer['peer_id'], db)

            for intro_data in pending:
                # Determine the other peer in the intro
                other_peer = intro_data['peer1_id'] if peer['peer_id'] == intro_data['peer2_id'] else intro_data['peer2_id']

                # Send hole punch packet
                self.send_hole_punch(peer['peer_id'], other_peer, t_ms, db)

                # Mark intro as processed
                intro.mark_processed(intro_data['intro_id'], peer['peer_id'], db)

        return {}

    def send_hole_punch(self, from_peer, to_peer, t_ms, db):
        """Send hole punch packet (creates outbound NAT mapping)."""
        # Create a small "punch" packet
        # This goes through queues.incoming.add() which creates the NAT mapping
```

**3.2 Hole punch packet type**

Simple packet that creates NAT mapping without payload:
```python
punch_packet = {
    'type': 'hole_punch',
    'from': from_peer_id,
    'to': to_peer_id,
    'timestamp': t_ms
}
```

### Phase 4: Keepalive

**4.1 Add NatKeepaliveJob**

```python
class NatKeepaliveJob(Job):
    """Send keepalive packets to refresh NAT mappings."""

    def __init__(self):
        # Run every 30 seconds (well within 2-minute TTL)
        super().__init__('nat_keepalive', every_ms=30_000)

    def run(self, t_ms: int, db: Any) -> dict:
        # For each local peer behind NAT
        # For each active NAT mapping
        # Send keepalive if mapping will expire soon
```

**4.2 Mapping expiry detection**

Track when mappings were last refreshed and warn/drop when TTL approaches.

### Phase 5: Integration Tests

**5.1 test_nat_enforcement.py**

```python
def test_packets_blocked_without_hole_punch():
    """Verify packets to NATed peer are dropped without hole punching."""

def test_hole_punch_enables_communication():
    """Verify hole punching allows packets through NAT."""

def test_mapping_expires_without_keepalive():
    """Verify NAT mappings expire and packets are blocked again."""

def test_symmetric_nat_cannot_punch():
    """Verify symmetric NAT prevents hole punching (needs relay)."""
```

**5.2 test_intro_triggers_hole_punch.py**

```python
def test_intro_event_triggers_punch():
    """Verify intro event processing triggers hole punch packets."""

def test_both_peers_must_punch():
    """Verify hole punch only succeeds when both peers send."""
```

## NAT Mode Behavior

| Mode | Outbound Mapping | Inbound Allowed | Punchable |
|------|------------------|-----------------|-----------|
| full_cone | Single port for all | Anyone | Yes (easy) |
| restricted | Per-destination IP | Same IP only | Yes |
| port_restricted | Per-destination IP:port | Same IP:port | Yes (default) |
| symmetric | Per-destination, random port | Same IP:port | No (needs relay) |

## Default Behavior

- `nat <n>` uses `port_restricted` mode (most common consumer NAT)
- 2-minute mapping TTL (conservative for testing)
- Keepalive every 30 seconds
- Intro events trigger automatic hole punching

## CLI Usage Examples

```
> nat 2
✓ account #2 (bob) is now behind NAT (port-restricted)
  mapping TTL: 120s, punchable: yes

> nat-status
NAT STATUS:
  1. alice      direct (no NAT)
  2. bob        port_restricted NAT, 3 active mappings
  3. charlie    direct (no NAT)

> nat 2 --off
✓ account #2 (bob) removed from NAT (direct connectivity)
```

## Testing Checklist

- [ ] `nat <n>` puts peer behind NAT
- [ ] Packets to NATed peer without hole punch are dropped
- [ ] Intro events are created and synced
- [ ] IntroProcessJob sends hole punch packets
- [ ] After hole punch, communication works
- [ ] Mappings expire after TTL
- [ ] Keepalive refreshes mappings
- [ ] Symmetric NAT blocks hole punching
