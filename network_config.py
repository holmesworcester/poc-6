"""Network simulator configuration.

Controls packet loss, latency, and other network characteristics for testing.
"""
from dataclasses import dataclass


@dataclass
class NetworkConfig:
    """Configuration for network simulation."""
    packet_loss_rate: float = 0.0  # 0.0 to 1.0 - probability of dropping packets
    latency_ms: int = 0             # Fixed latency in milliseconds
    max_packet_size: int = 600      # Maximum packet size in bytes


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
    global _config
    _config = NetworkConfig()
