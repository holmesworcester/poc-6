"""Network simulator configuration.

This module provides the public API for configuring and controlling the
network simulator. It delegates to the ns.py-based NetworkSimulator.
"""
from dataclasses import dataclass, field
from typing import Set, Optional, Dict
import logging

from simulator.nspy_network import (
    NetworkSimulator,
    NetworkConfig as NspyNetworkConfig,
)
from simulator.nat import NatConfig

log = logging.getLogger(__name__)


@dataclass
class NetworkConfig:
    """Configuration for network simulation.

    This is the public-facing config that gets translated to NspyNetworkConfig.
    """
    # Basic settings
    packet_loss_rate: float = 0.0  # 0.0 to 1.0 - probability of dropping packets
    latency_ms: int = 20            # Base latency in milliseconds (20ms = good wired connection)
    max_packet_size: int = 10000    # Maximum packet size in bytes

    # Jitter: adds random variation to latency (normal distribution)
    jitter_ms: int = 5              # Standard deviation of latency jitter in ms

    # Network partitions: completely block traffic to/from certain peers
    partitioned_peers: Set[str] = field(default_factory=set)

    # Burst loss: simulates correlated packet loss (packets tend to be lost in bursts)
    burst_loss_probability: float = 0.0  # Probability of entering burst loss state
    burst_loss_length: int = 3           # Number of consecutive packets lost in burst

    # Bandwidth limiting (1 Mbps = 125KB/s, realistic home upload speed)
    bandwidth_bytes_per_sec: Optional[int] = 125_000  # 1 Mbps upload


# Global simulator instance
_simulator: Optional[NetworkSimulator] = None

# Current config (must be defined before get_simulator for initialization)
_config = NetworkConfig()


def _to_nspy_config(config: NetworkConfig) -> NspyNetworkConfig:
    """Convert public NetworkConfig to internal NspyNetworkConfig."""
    return NspyNetworkConfig(
        latency_ms=float(config.latency_ms),
        jitter_ms=float(config.jitter_ms),
        packet_loss_rate=config.packet_loss_rate,
        burst_loss_probability=config.burst_loss_probability,
        burst_loss_length=config.burst_loss_length,
        bandwidth_bps=config.bandwidth_bytes_per_sec * 8 if config.bandwidth_bytes_per_sec else None,
        max_packet_size=config.max_packet_size,
    )


def get_simulator() -> NetworkSimulator:
    """Get the global simulator instance, creating if needed with default config."""
    global _simulator
    if _simulator is None:
        _simulator = NetworkSimulator(config=_to_nspy_config(_config))
    return _simulator


def set_network_config(config: NetworkConfig) -> None:
    """Set the global network configuration."""
    global _config
    _config = config
    sim = get_simulator()
    sim.configure(_to_nspy_config(config))

    # Apply partitions
    for peer_id in config.partitioned_peers:
        sim.partition_peer(peer_id)


def get_network_config() -> NetworkConfig:
    """Get the current network configuration."""
    return _config


# ============================================================================
# PARTITIONS
# ============================================================================

def partition_peer(peer_id: str) -> None:
    """Add a peer to the partition (block all traffic to/from)."""
    get_network_config().partitioned_peers.add(peer_id)
    get_simulator().partition_peer(peer_id)


def unpartition_peer(peer_id: str) -> None:
    """Remove a peer from the partition (restore traffic)."""
    get_network_config().partitioned_peers.discard(peer_id)
    get_simulator().unpartition_peer(peer_id)


def is_partitioned(peer_id: str) -> bool:
    """Check if a peer is partitioned."""
    return get_simulator().is_partitioned(peer_id)


# ============================================================================
# NAT SIMULATION
# ============================================================================

class PeerNatConfig:
    """NAT configuration for a specific peer.

    Setting mapping_ttl_ms updates the underlying NAT engine config.
    """

    def __init__(self, behind_nat: bool = False, nat_mode: str = 'port_restricted'):
        self.behind_nat = behind_nat
        self.nat_mode = nat_mode
        self._sim = None  # Set when returned from get_peer_nat

    @property
    def mapping_ttl_ms(self) -> int:
        if self._sim:
            return self._sim.nat_engine.config.mapping_ttl_ms
        return 120_000

    @mapping_ttl_ms.setter
    def mapping_ttl_ms(self, value: int) -> None:
        if self._sim:
            # Update the NAT engine config
            from simulator.nat import NatConfig
            old_config = self._sim.nat_engine.config
            self._sim.nat_engine.config = NatConfig(
                mode=old_config.mode,
                mapping_ttl_ms=value,
                hairpinning=old_config.hairpinning
            )


def set_peer_nat(peer_id: str, behind_nat: bool, nat_mode: str = 'port_restricted') -> None:
    """Put a peer behind NAT or remove from NAT."""
    sim = get_simulator()

    if behind_nat:
        # Register peer with NAT engine
        sim.register_peer(peer_id, behind_nat=True)
        log.info(f"nat: peer {peer_id[:20]}... now behind {nat_mode} NAT")
    else:
        # Register as direct peer
        sim.register_peer(peer_id, behind_nat=False)
        log.info(f"nat: peer {peer_id[:20]}... removed from NAT (direct)")


def get_peer_nat(peer_id: str) -> Optional[PeerNatConfig]:
    """Get NAT config for a peer, or None if not behind NAT.

    Returns a PeerNatConfig with mapping_ttl_ms that can be modified.
    Note: Modifying it affects the underlying NatEngine config.
    """
    sim = get_simulator()
    endpoint = sim.nat_engine.get_endpoint(peer_id)
    if endpoint and endpoint.behind_nat:
        # Return a wrapper that allows setting TTL
        config = PeerNatConfig(behind_nat=True)
        # Link to the actual TTL (modifications affect engine config)
        config._sim = sim  # type: ignore
        return config
    return None


def get_all_nat_peers() -> Dict[str, PeerNatConfig]:
    """Get all peers behind NAT."""
    sim = get_simulator()
    result = {}
    for peer_id, endpoint in sim.nat_engine.peer_endpoints.items():
        if endpoint.behind_nat:
            result[peer_id] = PeerNatConfig(behind_nat=True)
    return result


def is_behind_nat(peer_id: str) -> bool:
    """Check if a peer is behind NAT."""
    sim = get_simulator()
    endpoint = sim.nat_engine.get_endpoint(peer_id)
    return endpoint is not None and endpoint.behind_nat


def create_nat_mapping(from_peer: str, to_peer: str, t_ms: int) -> None:
    """Create or refresh a NAT mapping (outbound connection)."""
    sim = get_simulator()
    sim.nat_engine.create_outbound_mapping(from_peer, to_peer, t_ms)


def has_nat_mapping(to_peer: str, from_peer: str, t_ms: int) -> bool:
    """Check if there's a valid (non-expired) NAT mapping.

    For packet from from_peer to reach to_peer (who is behind NAT),
    to_peer must have previously sent to from_peer (creating the mapping).
    """
    sim = get_simulator()

    # Check if to_peer has a mapping to from_peer
    mappings = sim.nat_engine.nat_mappings.get(to_peer, [])
    for mapping in mappings:
        if mapping.dest_peer_id == from_peer:
            if not mapping.is_expired(t_ms, sim.nat_engine.config.mapping_ttl_ms):
                return True
    return False


def cleanup_expired_mappings(t_ms: int) -> int:
    """Remove all expired NAT mappings."""
    sim = get_simulator()
    return sim.nat_engine.cleanup_expired_mappings(t_ms)


def get_nat_mappings_for_peer(peer_id: str) -> list:
    """Get all active NAT mappings for a peer.

    Returns:
        List of (to_peer, mapping) tuples
    """
    sim = get_simulator()
    mappings = sim.nat_engine.nat_mappings.get(peer_id, [])
    return [(m.dest_peer_id, m) for m in mappings]


def reset_nat_state() -> None:
    """Reset all NAT state (for testing)."""
    sim = get_simulator()
    sim.nat_engine.peer_endpoints.clear()
    sim.nat_engine.nat_mappings.clear()


# ============================================================================
# BANDWIDTH (for stats compatibility)
# ============================================================================

def get_bandwidth_stats(t_ms: int) -> dict:
    """Get current bandwidth usage statistics."""
    cfg = get_network_config()
    sim = get_simulator()
    stats = sim.get_stats()

    if cfg.bandwidth_bytes_per_sec is None:
        return {
            'limit': None,
            'used': 0,
            'available': None,
            'utilization': 0.0,
        }

    return {
        'limit': cfg.bandwidth_bytes_per_sec,
        'used': 0,  # Token bucket doesn't track cumulative usage the same way
        'available': cfg.bandwidth_bytes_per_sec,
        'utilization': 0.0,
    }


def consume_bandwidth(bytes_count: int, t_ms: int) -> bool:
    """Bandwidth is now handled by token bucket in simulator.

    This always returns True - actual limiting happens during send().
    """
    return True


# ============================================================================
# RESET
# ============================================================================

def reset_network_config() -> None:
    """Reset to fast configuration (for testing).

    Uses 0 latency and unlimited bandwidth for fast test execution.
    The class defaults (20ms latency, 1Mbps bandwidth) are for realistic CLI demo.
    """
    global _config, _simulator
    # Use fast settings for tests (override class defaults)
    _config = NetworkConfig(
        latency_ms=0,
        jitter_ms=0,
        bandwidth_bytes_per_sec=None,  # Unlimited
    )
    if _simulator is not None:
        _simulator.reset()
    _simulator = None
