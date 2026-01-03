"""SimPy-based network simulator using ns.py components.

Provides realistic network simulation with:
- Token bucket bandwidth limiting
- Configurable latency with jitter
- Pluggable packet loss models (independent, Gilbert-Elliott, burst)
- NAT traversal simulation (via existing nat.py)
- In-memory operation (no SQLite overhead)
"""
import random
from dataclasses import dataclass, field
from typing import Callable, Any
import logging

import simpy
from ns.packet.packet import Packet
from ns.packet.sink import PacketSink
from ns.port.wire import Wire
from ns.shaper.token_bucket import TokenBucketShaper

from simulator.nat import NatEngine, NatConfig
from simulator.loss_models import independent_loss, SimpleBurstLoss, CombinedLoss

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NetworkConfig:
    """Immutable network configuration.

    All rates are in bits per second (bps) to match ns.py conventions.
    All sizes are in bytes.
    All times are in milliseconds.
    """
    # Latency settings
    latency_ms: float = 0.0
    jitter_ms: float = 0.0  # Standard deviation of gaussian jitter

    # Packet loss settings
    packet_loss_rate: float = 0.0  # Independent random loss [0.0, 1.0]
    burst_loss_probability: float = 0.0  # Probability of entering burst
    burst_loss_length: int = 3  # Consecutive packets dropped in burst

    # Bandwidth settings
    bandwidth_bps: int | None = None  # bits/sec, None = unlimited
    bucket_size_bytes: int = 65536  # Token bucket size (64KB default)

    # Packet settings
    max_packet_size: int = 10000  # Maximum packet size in bytes


@dataclass
class DeliveredPacket:
    """A packet that has been delivered through the network."""
    blob: bytes
    from_peer: str
    to_peer: str
    sent_at_ms: int
    delivered_at_ms: int


class PacketCollector:
    """Collects packets delivered through the network.

    This is the sink at the end of each link pipeline.
    """

    def __init__(self, env: simpy.Environment, simulator: 'NetworkSimulator'):
        self.env = env
        self.simulator = simulator
        self.packets_received = 0

    def put(self, packet: Packet) -> None:
        """Receive a delivered packet."""
        self.packets_received += 1

        # Extract our metadata from the packet
        blob = packet.payload
        from_peer = packet.src
        to_peer = packet.dst

        # Convert SimPy time back to milliseconds
        delivered_at_ms = int(self.env.now)

        delivered = DeliveredPacket(
            blob=blob,
            from_peer=from_peer,
            to_peer=to_peer,
            sent_at_ms=int(packet.time),
            delivered_at_ms=delivered_at_ms
        )

        self.simulator._on_packet_delivered(delivered)

        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                f"nspy: delivered {len(blob)}B from {from_peer[:12]}... "
                f"to {to_peer[:12]}... at t={delivered_at_ms}ms"
            )


class Link:
    """A unidirectional network link between two peers.

    Pipeline: [loss check] → [size check] → [token bucket] → [wire] → collector

    The token bucket and wire are ns.py components; loss and size checks
    are done before enqueueing.
    """

    def __init__(self,
                 env: simpy.Environment,
                 collector: PacketCollector,
                 config: NetworkConfig,
                 seed: int = None):
        self.env = env
        self.config = config
        self._rng = random.Random(seed)
        self._packet_id = 0

        # Build loss model
        self._loss_model = self._build_loss_model(config, seed)

        # Create delay distribution function
        def delay_dist() -> float:
            if config.jitter_ms == 0:
                return config.latency_ms
            delay = self._rng.gauss(config.latency_ms, config.jitter_ms)
            return max(0.0, delay)  # Clamp to non-negative

        # Create wire (adds propagation delay)
        # Note: Wire also supports loss_dist, but we handle loss separately
        # for more control and to match our API
        self.wire = Wire(
            env=env,
            delay_dist=delay_dist,
            wire_id=0,
            debug=False
        )
        self.wire.out = collector

        # Create token bucket shaper if bandwidth limited
        # Note: TokenBucketShaper rate is in bits per simulation time unit.
        # Our simulation uses milliseconds, so convert bps to bits/ms (divide by 1000).
        if config.bandwidth_bps is not None:
            self.shaper = TokenBucketShaper(
                env=env,
                rate=config.bandwidth_bps / 1000.0,  # bits/sec → bits/ms
                bucket_size=config.bucket_size_bytes,
                debug=False
            )
            self.shaper.out = self.wire
            self._entry_point = self.shaper
        else:
            self.shaper = None
            self._entry_point = self.wire

        self.packets_sent = 0
        self.packets_dropped_loss = 0
        self.packets_dropped_size = 0

    def _build_loss_model(self, config: NetworkConfig, seed: int) -> Callable[[int], float]:
        """Build combined loss model from config."""
        models = []

        if config.packet_loss_rate > 0:
            models.append(independent_loss(config.packet_loss_rate))

        if config.burst_loss_probability > 0:
            models.append(SimpleBurstLoss(
                burst_probability=config.burst_loss_probability,
                burst_length=config.burst_loss_length,
                seed=seed
            ))

        if not models:
            return lambda packet_id: 0.0

        if len(models) == 1:
            return models[0]

        return CombinedLoss(*models)

    def send(self, packet: Packet) -> bool:
        """Send a packet through this link.

        Returns True if packet was accepted, False if dropped.
        """
        # Check packet size
        if packet.size > self.config.max_packet_size:
            self.packets_dropped_size += 1
            log.debug(f"nspy: dropped oversized packet: {packet.size}B > {self.config.max_packet_size}B")
            return False

        # Check loss model
        loss_prob = self._loss_model(packet.packet_id)
        if self._rng.random() < loss_prob:
            self.packets_dropped_loss += 1
            log.debug(f"nspy: dropped packet due to loss (p={loss_prob:.2f})")
            return False

        # Send through pipeline
        self._entry_point.put(packet)
        self.packets_sent += 1
        return True


class NetworkSimulator:
    """SimPy-based network simulator.

    Provides the same conceptual API as the SQLite-based queues.incoming,
    but uses ns.py for realistic network simulation.

    Time is in milliseconds throughout the public API.
    """

    def __init__(self, config: NetworkConfig = None, nat_config: NatConfig = None):
        """Initialize network simulator.

        Args:
            config: Network configuration (latency, loss, bandwidth)
            nat_config: NAT configuration
        """
        self.env = simpy.Environment()
        self.config = config or NetworkConfig()
        self.nat_engine = NatEngine(nat_config or NatConfig())

        self._collector = PacketCollector(self.env, self)
        self._links: dict[tuple[str, str], Link] = {}
        self._delivered: list[DeliveredPacket] = []
        self._partitioned_peers: set[str] = set()
        self._packet_id_counter = 0
        self._seed = 42  # Default seed for reproducibility

    def configure(self, config: NetworkConfig) -> None:
        """Update network configuration.

        This clears existing links; they will be recreated on next send.
        """
        self.config = config
        self._links.clear()

    def set_seed(self, seed: int) -> None:
        """Set random seed for reproducibility."""
        self._seed = seed
        # Clear links so they get recreated with new seed
        self._links.clear()

    def register_peer(self,
                      peer_id: str,
                      local_ip: str = '127.0.0.1',
                      local_port: int = 5000,
                      public_ip: str = None,
                      public_port: int = None,
                      behind_nat: bool = False) -> None:
        """Register a peer with the network simulator.

        Args:
            peer_id: Unique peer identifier
            local_ip: Internal IP address
            local_port: Internal port
            public_ip: External IP (auto-assigned if None and behind_nat)
            public_port: External port (defaults to local_port)
            behind_nat: Whether peer is behind NAT
        """
        if public_ip is None:
            public_ip = local_ip if not behind_nat else f"203.0.113.{abs(hash(peer_id)) % 256}"
        if public_port is None:
            public_port = local_port

        self.nat_engine.register_peer(
            peer_id=peer_id,
            local_ip=local_ip,
            local_port=local_port,
            public_ip=public_ip,
            public_port=public_port,
            behind_nat=behind_nat
        )

    def send(self,
             from_peer: str,
             to_peer: str,
             blob: bytes,
             t_ms: int) -> bool:
        """Send a packet through the simulated network.

        Args:
            from_peer: Source peer ID
            to_peer: Destination peer ID
            blob: Packet payload
            t_ms: Current simulation time in milliseconds

        Returns:
            True if packet was accepted into network, False if dropped immediately
        """
        # Advance simulation time if needed
        if t_ms > self.env.now:
            self.env.run(until=t_ms)

        # Check partitions
        if from_peer in self._partitioned_peers:
            log.debug(f"nspy: dropped packet from partitioned peer {from_peer[:12]}...")
            return False
        if to_peer in self._partitioned_peers:
            log.debug(f"nspy: dropped packet to partitioned peer {to_peer[:12]}...")
            return False

        # Check NAT - create mapping for sender if behind NAT
        from_endpoint = self.nat_engine.get_endpoint(from_peer)
        to_endpoint = self.nat_engine.get_endpoint(to_peer)

        if from_endpoint and from_endpoint.behind_nat:
            self.nat_engine.create_outbound_mapping(from_peer, to_peer, t_ms)

        # Check if destination is behind NAT and has mapping
        if to_endpoint and to_endpoint.behind_nat:
            # Check if there's a mapping allowing this packet through
            has_mapping = self._check_nat_mapping(to_peer, from_peer, t_ms)
            if not has_mapping:
                log.debug(f"nspy: dropped packet - {to_peer[:12]}... behind NAT, no hole punch")
                return False

        # Get or create link
        link_key = (from_peer, to_peer)
        if link_key not in self._links:
            # Create unique seed for this link
            link_seed = hash((self._seed, from_peer, to_peer)) & 0xFFFFFFFF
            self._links[link_key] = Link(
                env=self.env,
                collector=self._collector,
                config=self.config,
                seed=link_seed
            )

        link = self._links[link_key]

        # Create ns.py Packet
        self._packet_id_counter += 1
        packet = Packet(
            time=t_ms,  # Send time in ms
            size=len(blob),
            packet_id=self._packet_id_counter,
            src=from_peer,
            dst=to_peer,
            payload=blob
        )

        return link.send(packet)

    def _check_nat_mapping(self, nat_peer: str, other_peer: str, t_ms: int) -> bool:
        """Check if NAT peer has a valid mapping for communication with other_peer."""
        # Look for outbound mapping from nat_peer to other_peer
        mappings = self.nat_engine.nat_mappings.get(nat_peer, [])
        for mapping in mappings:
            if mapping.dest_peer_id == other_peer:
                if not mapping.is_expired(t_ms, self.nat_engine.config.mapping_ttl_ms):
                    return True
        return False

    def _on_packet_delivered(self, packet: DeliveredPacket) -> None:
        """Called when a packet completes its journey through the network."""
        self._delivered.append(packet)

    def advance_to(self, t_ms: int) -> None:
        """Advance simulation time, processing any pending events.

        Args:
            t_ms: Target time in milliseconds
        """
        # SimPy's env.run(until=T) processes events at times < T, not <= T
        # To include events at exactly t_ms, we run until t_ms + epsilon
        target = t_ms + 0.001

        if target > self.env.now:
            self.env.run(until=target)

        # Clean up expired NAT mappings
        self.nat_engine.cleanup_expired_mappings(t_ms)

    def drain(self, max_count: int = None) -> list[bytes]:
        """Get delivered packets, removing them from the queue.

        Args:
            max_count: Maximum number of packets to return (None = all)

        Returns:
            List of packet payloads (bytes)
        """
        if max_count is None:
            result = [p.blob for p in self._delivered]
            self._delivered.clear()
        else:
            result = [p.blob for p in self._delivered[:max_count]]
            self._delivered = self._delivered[max_count:]

        if result:
            log.info(f"nspy: drained {len(result)} packets")

        return result

    def drain_with_metadata(self, max_count: int = None) -> list[DeliveredPacket]:
        """Get delivered packets with full metadata.

        Args:
            max_count: Maximum number of packets to return (None = all)

        Returns:
            List of DeliveredPacket objects
        """
        if max_count is None:
            result = list(self._delivered)
            self._delivered.clear()
        else:
            result = self._delivered[:max_count]
            self._delivered = self._delivered[max_count:]

        return result

    def partition_peer(self, peer_id: str) -> None:
        """Block all traffic to/from a peer."""
        self._partitioned_peers.add(peer_id)
        log.info(f"nspy: partitioned peer {peer_id[:20]}...")

    def unpartition_peer(self, peer_id: str) -> None:
        """Restore traffic to/from a peer."""
        self._partitioned_peers.discard(peer_id)
        log.info(f"nspy: unpartitioned peer {peer_id[:20]}...")

    def is_partitioned(self, peer_id: str) -> bool:
        """Check if peer is partitioned."""
        return peer_id in self._partitioned_peers

    def get_stats(self) -> dict:
        """Get simulation statistics."""
        total_sent = sum(link.packets_sent for link in self._links.values())
        total_dropped_loss = sum(link.packets_dropped_loss for link in self._links.values())
        total_dropped_size = sum(link.packets_dropped_size for link in self._links.values())

        return {
            'current_time_ms': int(self.env.now),
            'packets_sent': total_sent,
            'packets_dropped_loss': total_dropped_loss,
            'packets_dropped_size': total_dropped_size,
            'packets_pending': len(self._delivered),
            'links_created': len(self._links),
            'peers_registered': len(self.nat_engine.peer_endpoints),
            'peers_partitioned': len(self._partitioned_peers),
        }

    def reset(self) -> None:
        """Reset simulator to initial state."""
        self.env = simpy.Environment()
        self._collector = PacketCollector(self.env, self)
        self._links.clear()
        self._delivered.clear()
        self._partitioned_peers.clear()
        self._packet_id_counter = 0
        self.nat_engine = NatEngine(self.nat_engine.config)
        log.info("nspy: reset")


# ============================================================================
# Convenience factory functions
# ============================================================================

def create_simulator(
    latency_ms: float = 0.0,
    jitter_ms: float = 0.0,
    packet_loss_rate: float = 0.0,
    bandwidth_kbps: int = None,
    **kwargs
) -> NetworkSimulator:
    """Create a network simulator with common settings.

    Args:
        latency_ms: Base latency in milliseconds
        jitter_ms: Jitter (standard deviation) in milliseconds
        packet_loss_rate: Random packet loss rate [0.0, 1.0]
        bandwidth_kbps: Bandwidth limit in kilobits/sec (None = unlimited)
        **kwargs: Additional NetworkConfig parameters

    Returns:
        Configured NetworkSimulator
    """
    bandwidth_bps = bandwidth_kbps * 1000 if bandwidth_kbps else None

    config = NetworkConfig(
        latency_ms=latency_ms,
        jitter_ms=jitter_ms,
        packet_loss_rate=packet_loss_rate,
        bandwidth_bps=bandwidth_bps,
        **kwargs
    )

    return NetworkSimulator(config=config)


# ============================================================================
# Network presets for common scenarios
# ============================================================================

PRESETS = {
    'lan': NetworkConfig(latency_ms=1, jitter_ms=0.5),
    'broadband': NetworkConfig(latency_ms=30, jitter_ms=5, packet_loss_rate=0.001),
    'mobile_4g': NetworkConfig(latency_ms=50, jitter_ms=20, packet_loss_rate=0.01),
    'mobile_3g': NetworkConfig(latency_ms=200, jitter_ms=50, packet_loss_rate=0.05),
    'satellite': NetworkConfig(latency_ms=600, jitter_ms=50, packet_loss_rate=0.01),
    'lossy_wifi': NetworkConfig(
        latency_ms=20,
        jitter_ms=30,
        packet_loss_rate=0.02,
        burst_loss_probability=0.05,
        burst_loss_length=3
    ),
}


def create_preset(name: str) -> NetworkSimulator:
    """Create simulator with a named preset configuration.

    Available presets: lan, broadband, mobile_4g, mobile_3g, satellite, lossy_wifi
    """
    if name not in PRESETS:
        raise ValueError(f"Unknown preset: {name}. Available: {list(PRESETS.keys())}")

    return NetworkSimulator(config=PRESETS[name])
