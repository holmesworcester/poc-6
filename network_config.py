"""Network simulator configuration.

Controls packet loss, latency, and other network characteristics for testing.
"""
from dataclasses import dataclass, field
from typing import Set, Optional, Dict, Tuple
import logging

log = logging.getLogger(__name__)


@dataclass
class NetworkConfig:
    """Configuration for network simulation."""
    # Basic settings
    packet_loss_rate: float = 0.0  # 0.0 to 1.0 - probability of dropping packets
    latency_ms: int = 0             # Base latency in milliseconds
    max_packet_size: int = 10000    # Maximum packet size in bytes (lower to ~600 for realistic UDP simulation)

    # Jitter: adds random variation to latency (normal distribution)
    jitter_ms: int = 0              # Standard deviation of latency jitter in ms

    # Network partitions: completely block traffic to/from certain peers
    partitioned_peers: Set[str] = field(default_factory=set)

    # Burst loss: simulates correlated packet loss (packets tend to be lost in bursts)
    burst_loss_probability: float = 0.0  # Probability of entering burst loss state
    burst_loss_length: int = 3           # Number of consecutive packets lost in burst

    # Bandwidth limiting
    bandwidth_bytes_per_sec: Optional[int] = None  # None = unlimited


# Global network configuration
_config = NetworkConfig()


def set_network_config(config: NetworkConfig) -> None:
    """Set the global network configuration."""
    global _config
    _config = config


def get_network_config() -> NetworkConfig:
    """Get the current network configuration."""
    return _config


# Burst loss state: tracks how many more packets should be dropped in current burst
_burst_loss_remaining = 0


def check_burst_loss() -> bool:
    """Check if current packet should be dropped due to burst loss.

    Returns True if packet should be dropped.
    Manages burst state: enters burst mode probabilistically, then drops
    consecutive packets until burst ends.
    """
    global _burst_loss_remaining
    cfg = get_network_config()

    # If in burst mode, drop packet and decrement counter
    if _burst_loss_remaining > 0:
        _burst_loss_remaining -= 1
        return True

    # Check if we should enter burst mode
    import random
    if random.random() < cfg.burst_loss_probability:
        _burst_loss_remaining = cfg.burst_loss_length - 1  # -1 because we drop this one
        return True

    return False


def partition_peer(peer_id: str) -> None:
    """Add a peer to the partition (block all traffic to/from)."""
    get_network_config().partitioned_peers.add(peer_id)


def unpartition_peer(peer_id: str) -> None:
    """Remove a peer from the partition (restore traffic)."""
    get_network_config().partitioned_peers.discard(peer_id)


def is_partitioned(peer_id: str) -> bool:
    """Check if a peer is partitioned."""
    return peer_id in get_network_config().partitioned_peers


def calculate_latency() -> int:
    """Calculate latency with jitter applied.

    Returns base latency +/- random jitter (clamped to >= 0).
    """
    import random
    cfg = get_network_config()

    if cfg.jitter_ms == 0:
        return cfg.latency_ms

    # Normal distribution with mean=latency_ms, stddev=jitter_ms
    jittered = int(random.gauss(cfg.latency_ms, cfg.jitter_ms))
    return max(0, jittered)  # Clamp to non-negative


# ============================================================================
# NAT SIMULATION
# ============================================================================

@dataclass
class PeerNatConfig:
    """NAT configuration for a specific peer."""
    behind_nat: bool = False
    nat_mode: str = 'port_restricted'  # 'full_cone', 'restricted', 'port_restricted', 'symmetric'
    mapping_ttl_ms: int = 120_000  # 2 minutes - conservative for testing


@dataclass
class NatMapping:
    """Active NAT mapping for a peer."""
    from_peer: str
    to_peer: str
    created_at_ms: int
    last_used_ms: int

    def is_expired(self, current_time_ms: int, ttl_ms: int) -> bool:
        """Check if mapping has expired."""
        return (current_time_ms - self.last_used_ms) > ttl_ms


# Per-peer NAT configs: peer_id -> PeerNatConfig
_peer_nat_configs: Dict[str, PeerNatConfig] = {}

# NAT mappings: (from_peer, to_peer) -> NatMapping
_nat_mappings: Dict[Tuple[str, str], NatMapping] = {}


def set_peer_nat(peer_id: str, behind_nat: bool, nat_mode: str = 'port_restricted') -> None:
    """Put a peer behind NAT or remove from NAT.

    Args:
        peer_id: The peer ID
        behind_nat: True to put behind NAT, False to remove
        nat_mode: NAT mode ('full_cone', 'restricted', 'port_restricted', 'symmetric')
    """
    if behind_nat:
        _peer_nat_configs[peer_id] = PeerNatConfig(
            behind_nat=True,
            nat_mode=nat_mode
        )
        log.info(f"nat: peer {peer_id[:20]}... now behind {nat_mode} NAT")
    else:
        if peer_id in _peer_nat_configs:
            del _peer_nat_configs[peer_id]
        log.info(f"nat: peer {peer_id[:20]}... removed from NAT (direct)")


def get_peer_nat(peer_id: str) -> Optional[PeerNatConfig]:
    """Get NAT config for a peer, or None if not behind NAT."""
    return _peer_nat_configs.get(peer_id)


def is_behind_nat(peer_id: str) -> bool:
    """Check if a peer is behind NAT."""
    config = _peer_nat_configs.get(peer_id)
    return config is not None and config.behind_nat


def get_all_nat_peers() -> Dict[str, PeerNatConfig]:
    """Get all peers behind NAT."""
    return dict(_peer_nat_configs)


def create_nat_mapping(from_peer: str, to_peer: str, t_ms: int) -> None:
    """Create or refresh a NAT mapping (outbound connection).

    When a peer behind NAT sends a packet, it creates a mapping that allows
    the destination to send packets back.

    Args:
        from_peer: Peer sending the packet (behind NAT)
        to_peer: Destination peer
        t_ms: Current timestamp
    """
    key = (from_peer, to_peer)

    if key in _nat_mappings:
        # Refresh existing mapping
        _nat_mappings[key].last_used_ms = t_ms
        log.debug(f"nat: refreshed mapping {from_peer[:12]}... -> {to_peer[:12]}...")
    else:
        # Create new mapping
        _nat_mappings[key] = NatMapping(
            from_peer=from_peer,
            to_peer=to_peer,
            created_at_ms=t_ms,
            last_used_ms=t_ms
        )
        log.info(f"nat: created mapping {from_peer[:12]}... -> {to_peer[:12]}...")


def has_nat_mapping(from_peer: str, to_peer: str, t_ms: int) -> bool:
    """Check if there's a valid (non-expired) NAT mapping.

    For packet from to_peer to reach from_peer (who is behind NAT),
    from_peer must have previously sent to to_peer (creating the mapping).

    Args:
        from_peer: Peer behind NAT (destination of incoming packet)
        to_peer: Peer sending the packet (source)
        t_ms: Current timestamp

    Returns:
        True if there's a valid mapping allowing the packet through
    """
    nat_config = get_peer_nat(from_peer)
    if not nat_config:
        return True  # Not behind NAT, always allow

    key = (from_peer, to_peer)
    mapping = _nat_mappings.get(key)

    if not mapping:
        return False  # No mapping exists

    if mapping.is_expired(t_ms, nat_config.mapping_ttl_ms):
        # Mapping expired - remove it
        del _nat_mappings[key]
        log.debug(f"nat: mapping expired {from_peer[:12]}... -> {to_peer[:12]}...")
        return False

    return True


def get_nat_mappings_for_peer(peer_id: str) -> list:
    """Get all active NAT mappings for a peer.

    Returns:
        List of (to_peer, mapping) tuples
    """
    result = []
    for (from_peer, to_peer), mapping in _nat_mappings.items():
        if from_peer == peer_id:
            result.append((to_peer, mapping))
    return result


def cleanup_expired_mappings(t_ms: int) -> int:
    """Remove all expired NAT mappings.

    Returns:
        Number of mappings removed
    """
    expired = []
    for key, mapping in _nat_mappings.items():
        nat_config = get_peer_nat(mapping.from_peer)
        ttl = nat_config.mapping_ttl_ms if nat_config else 120_000
        if mapping.is_expired(t_ms, ttl):
            expired.append(key)

    for key in expired:
        del _nat_mappings[key]

    if expired:
        log.debug(f"nat: cleaned up {len(expired)} expired mappings")

    return len(expired)


def reset_nat_state() -> None:
    """Reset all NAT state (for testing)."""
    global _peer_nat_configs, _nat_mappings
    _peer_nat_configs = {}
    _nat_mappings = {}


def reset_network_config() -> None:
    """Reset to default configuration (for testing)."""
    global _config, _burst_loss_remaining, _peer_nat_configs, _nat_mappings
    _config = NetworkConfig()
    _burst_loss_remaining = 0
    _peer_nat_configs = {}
    _nat_mappings = {}
