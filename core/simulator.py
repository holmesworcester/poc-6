"""Network simulator for testing network conditions.

Provides simulated network conditions including:
- Latency with jitter
- Packet loss (random and burst)
- Network partitions
- NAT traversal simulation

Integrates with core/transport.py as an optional transport layer.

Usage:
    from core import transport, simulator

    # Create and configure simulator
    sim = simulator.NetworkSimulator(
        latency_ms=50,
        packet_loss_rate=0.05,
    )

    # Enable simulator mode
    transport.set_simulator(sim)

    # Now loopback_transfer() will use the simulator
    transport.loopback_transfer()

    # Disable when done
    transport.set_simulator(None)
"""
import random
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class NetworkConfig:
    """Configuration for network simulation."""

    # Latency settings
    latency_ms: float = 1.0  # Base latency (use >= 1, not 0)
    jitter_ms: float = 0.0   # Standard deviation of gaussian jitter

    # Packet loss settings
    packet_loss_rate: float = 0.0  # Independent random loss [0.0, 1.0]
    burst_loss_probability: float = 0.0  # Probability of entering burst
    burst_loss_length: int = 3  # Consecutive packets dropped in burst

    # Bandwidth settings (optional, bytes/sec)
    bandwidth_bytes_per_sec: Optional[int] = None

    # Packet settings
    max_packet_size: int = 65535  # Maximum packet size in bytes


@dataclass
class PendingPacket:
    """A packet waiting to be delivered."""
    blob: bytes
    from_addr: tuple
    to_addr: tuple
    deliver_at_ms: int


class NetworkSimulator:
    """Simulates network conditions for testing.

    Packets are added via add(), and delivered via drain() when their
    scheduled delivery time has passed.
    """

    def __init__(self, config: NetworkConfig = None, seed: int = None):
        self.config = config or NetworkConfig()
        self._pending: list[PendingPacket] = []
        self._in_burst = False
        self._burst_remaining = 0
        self._partitioned_addrs: set[tuple] = set()

        # NAT simulation: mapping of (internal_addr) -> external_addr
        # Packets from external can only reach internal if mapping exists
        self._nat_mappings: dict[tuple, tuple] = {}  # internal -> external
        self._nat_enabled_addrs: set[tuple] = set()  # addresses behind NAT
        self._nat_ttl_ms = 30000  # NAT mapping timeout
        self._nat_mapping_created_at: dict[tuple, int] = {}  # internal -> created_at

        # Own Random instance for isolation from other tests in parallel execution
        self._rng = random.Random(seed)

        # Stats
        self.packets_sent = 0
        self.packets_dropped = 0
        self.packets_delivered = 0

    def configure(self, config: NetworkConfig) -> None:
        """Update configuration."""
        self.config = config

    def reset(self, seed: int = None) -> None:
        """Reset simulator state."""
        self._pending.clear()
        self._in_burst = False
        self._burst_remaining = 0
        self._partitioned_addrs.clear()
        self._nat_mappings.clear()
        self._nat_enabled_addrs.clear()
        self._nat_mapping_created_at.clear()
        if seed is not None:
            self._rng = random.Random(seed)
        self.packets_sent = 0
        self.packets_dropped = 0
        self.packets_delivered = 0

    # === Partition simulation ===

    def partition(self, addr: tuple) -> None:
        """Block all traffic to/from an address."""
        self._partitioned_addrs.add(addr)
        log.debug(f"simulator: partitioned {addr}")

    def heal_partition(self, addr: tuple) -> None:
        """Restore traffic to/from an address."""
        self._partitioned_addrs.discard(addr)
        log.debug(f"simulator: healed partition for {addr}")

    def heal_all_partitions(self) -> None:
        """Restore all partitioned addresses."""
        self._partitioned_addrs.clear()
        log.debug("simulator: healed all partitions")

    # === NAT simulation ===

    def enable_nat(self, internal_addr: tuple) -> None:
        """Put an address behind NAT (blocks incoming until hole-punched)."""
        self._nat_enabled_addrs.add(internal_addr)
        log.debug(f"simulator: enabled NAT for {internal_addr}")

    def disable_nat(self, internal_addr: tuple) -> None:
        """Remove NAT from an address."""
        self._nat_enabled_addrs.discard(internal_addr)
        self._nat_mappings.pop(internal_addr, None)
        self._nat_mapping_created_at.pop(internal_addr, None)
        log.debug(f"simulator: disabled NAT for {internal_addr}")

    def create_nat_mapping(self, internal_addr: tuple, external_addr: tuple, t_ms: int) -> None:
        """Create a NAT mapping (hole punch)."""
        self._nat_mappings[internal_addr] = external_addr
        self._nat_mapping_created_at[internal_addr] = t_ms
        log.debug(f"simulator: NAT mapping {internal_addr} -> {external_addr}")

    def _check_nat(self, from_addr: tuple, to_addr: tuple, t_ms: int) -> bool:
        """Check if packet can pass NAT. Returns True if allowed."""
        # If destination is not behind NAT, allow
        if to_addr not in self._nat_enabled_addrs:
            return True

        # Check if there's a valid mapping from to_addr to from_addr
        if to_addr in self._nat_mappings:
            mapped_external = self._nat_mappings[to_addr]
            created_at = self._nat_mapping_created_at.get(to_addr, 0)

            # Check TTL
            if t_ms - created_at > self._nat_ttl_ms:
                # Mapping expired
                del self._nat_mappings[to_addr]
                del self._nat_mapping_created_at[to_addr]
                log.debug(f"simulator: NAT mapping expired for {to_addr}")
                return False

            # Check if from_addr matches the mapping
            if mapped_external == from_addr:
                return True

        log.debug(f"simulator: NAT blocked {from_addr} -> {to_addr}")
        return False

    # === Packet processing ===

    def add(self, blob: bytes, from_addr: tuple, to_addr: tuple, t_ms: int) -> bool:
        """Add a packet to the simulator.

        Returns True if packet was accepted, False if dropped.
        """
        self.packets_sent += 1

        # Check packet size
        if len(blob) > self.config.max_packet_size:
            log.debug(f"simulator: dropped oversized packet ({len(blob)} > {self.config.max_packet_size})")
            self.packets_dropped += 1
            return False

        # Check partitions
        if from_addr in self._partitioned_addrs or to_addr in self._partitioned_addrs:
            log.debug(f"simulator: dropped packet due to partition")
            self.packets_dropped += 1
            return False

        # Check NAT
        if not self._check_nat(from_addr, to_addr, t_ms):
            self.packets_dropped += 1
            return False

        # Check burst loss
        if self._in_burst:
            self._burst_remaining -= 1
            if self._burst_remaining <= 0:
                self._in_burst = False
            log.debug(f"simulator: dropped packet (burst loss, {self._burst_remaining} remaining)")
            self.packets_dropped += 1
            return False

        # Check if entering burst
        if self.config.burst_loss_probability > 0:
            if self._rng.random() < self.config.burst_loss_probability:
                self._in_burst = True
                self._burst_remaining = self.config.burst_loss_length - 1
                log.debug(f"simulator: entering burst loss, dropping packet")
                self.packets_dropped += 1
                return False

        # Check random loss
        if self.config.packet_loss_rate > 0:
            if self._rng.random() < self.config.packet_loss_rate:
                log.debug(f"simulator: dropped packet (random loss)")
                self.packets_dropped += 1
                return False

        # Calculate delivery time
        latency = self.config.latency_ms
        if self.config.jitter_ms > 0:
            latency += self._rng.gauss(0, self.config.jitter_ms)
            latency = max(1, latency)  # Ensure non-negative

        deliver_at = t_ms + int(latency)

        # Add to pending queue
        self._pending.append(PendingPacket(
            blob=blob,
            from_addr=from_addr,
            to_addr=to_addr,
            deliver_at_ms=deliver_at,
        ))

        log.debug(f"simulator: queued packet for delivery at t={deliver_at}ms (latency={latency:.1f}ms)")
        return True

    def drain(self, t_ms: int, max_packets: int = 100) -> list[tuple[bytes, tuple]]:
        """Drain packets ready for delivery.

        Returns list of (blob, from_addr) tuples for packets whose
        delivery time has passed.
        """
        ready = []
        still_pending = []

        for pkt in self._pending:
            if pkt.deliver_at_ms <= t_ms and len(ready) < max_packets:
                ready.append((pkt.blob, pkt.from_addr))
                self.packets_delivered += 1
            else:
                still_pending.append(pkt)

        self._pending = still_pending

        if ready:
            log.debug(f"simulator: delivered {len(ready)} packets at t={t_ms}ms")

        return ready

    def pending_count(self) -> int:
        """Number of packets waiting for delivery."""
        return len(self._pending)

    def stats(self) -> dict:
        """Get simulator statistics."""
        return {
            'sent': self.packets_sent,
            'dropped': self.packets_dropped,
            'delivered': self.packets_delivered,
            'pending': len(self._pending),
            'drop_rate': self.packets_dropped / self.packets_sent if self.packets_sent > 0 else 0,
        }


# Global simulator instance (None = disabled)
_simulator: Optional[NetworkSimulator] = None


def get_simulator() -> Optional[NetworkSimulator]:
    """Get the global simulator instance."""
    return _simulator


def set_simulator(sim: Optional[NetworkSimulator]) -> None:
    """Set the global simulator instance."""
    global _simulator
    _simulator = sim
    if sim:
        log.info("simulator: enabled")
    else:
        log.info("simulator: disabled")


def reset() -> None:
    """Reset the global simulator."""
    global _simulator
    if _simulator:
        _simulator.reset()
    _simulator = None
