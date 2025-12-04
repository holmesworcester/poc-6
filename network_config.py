"""Network simulator configuration.

Controls packet loss, latency, and other network characteristics for testing.
"""
from dataclasses import dataclass, field
from typing import Set, Optional


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


def reset_network_config() -> None:
    """Reset to default configuration (for testing)."""
    global _config, _burst_loss_remaining, _bandwidth_tokens, _bandwidth_last_refill_ms
    _config = NetworkConfig()
    _burst_loss_remaining = 0
    _bandwidth_tokens = 0
    _bandwidth_last_refill_ms = 0


# Burst loss state: tracks how many more packets should be dropped in current burst
_burst_loss_remaining = 0

# Bandwidth limiting state (token bucket)
_bandwidth_tokens = 0
_bandwidth_last_refill_ms = 0


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


def calculate_delivery_time(size_bytes: int, t_ms: int) -> int:
    """Calculate when a packet should be delivered, accounting for bandwidth.

    Uses a token bucket algorithm: tokens refill at the configured bandwidth rate,
    and packets wait until enough tokens accumulate. This simulates realistic
    bandwidth constraints where large packets take longer to transmit.

    Args:
        size_bytes: Size of the packet in bytes
        t_ms: Current simulation time in milliseconds

    Returns:
        Delivery time in milliseconds (t_ms + latency + bandwidth_delay)
    """
    global _bandwidth_tokens, _bandwidth_last_refill_ms
    cfg = get_network_config()

    # Base latency with jitter
    latency = calculate_latency()

    # If no bandwidth limit, just use latency
    if cfg.bandwidth_bytes_per_sec is None:
        return t_ms + latency

    # Refill tokens since last packet
    if _bandwidth_last_refill_ms > 0:
        elapsed_ms = t_ms - _bandwidth_last_refill_ms
        if elapsed_ms > 0:
            refill = int(cfg.bandwidth_bytes_per_sec * elapsed_ms / 1000)
            # Cap at 1 second worth of tokens (burst allowance)
            _bandwidth_tokens = min(
                _bandwidth_tokens + refill,
                cfg.bandwidth_bytes_per_sec
            )
    _bandwidth_last_refill_ms = t_ms

    # Calculate wait time if not enough tokens
    if size_bytes <= _bandwidth_tokens:
        _bandwidth_tokens -= size_bytes
        bandwidth_delay = 0
    else:
        # Need to wait for more tokens to accumulate
        bytes_needed = size_bytes - _bandwidth_tokens
        bandwidth_delay = int(bytes_needed * 1000 / cfg.bandwidth_bytes_per_sec)
        _bandwidth_tokens = 0

    return t_ms + latency + bandwidth_delay
