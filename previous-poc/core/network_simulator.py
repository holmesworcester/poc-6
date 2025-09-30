"""UDP Network Simulator for testing distributed systems."""

import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Any


@dataclass
class NetworkConfig:
    """Configuration for network simulation."""
    packet_loss_rate: float = 0.0  # 0.0 to 1.0
    latency_ms: int = 0  # milliseconds
    max_packet_size: int = 600  # bytes


@dataclass
class PendingPacket:
    """A packet waiting to be delivered."""
    raw_data: bytes
    origin_ip: str
    origin_port: int
    dest_ip: str
    dest_port: int
    delivery_time_ms: int


class UDPNetworkSimulator:
    """
    Simulates UDP network with packet loss, latency, and size limits.

    This is a dumb pipe - it doesn't know about addresses or identities.
    It just moves packets based on physical constraints.
    """

    def __init__(self, config: Optional[NetworkConfig] = None):
        self.config = config or NetworkConfig()
        self.pending_packets: List[PendingPacket] = []
        self.current_time_ms = 0

    def send(self, origin_ip: str, origin_port: int, dest_ip: str, dest_port: int,
             data: bytes, current_time_ms: Optional[int] = None) -> bool:
        """
        Send a packet through the simulator.

        Args:
            origin_ip: Source IP address
            origin_port: Source port
            dest_ip: Destination IP address
            dest_port: Destination port
            data: Raw packet data
            current_time_ms: Current time in milliseconds

        Returns:
            True if packet was queued (not dropped), False if dropped
        """
        if current_time_ms is not None:
            self.current_time_ms = current_time_ms

        # Check packet size
        if len(data) > self.config.max_packet_size:
            return False  # Drop oversized packet

        # Apply packet loss
        if random.random() < self.config.packet_loss_rate:
            return False  # Drop packet

        # Calculate delivery time
        delivery_time_ms = self.current_time_ms + self.config.latency_ms

        # Queue packet for delivery
        self.pending_packets.append(PendingPacket(
            raw_data=data,
            origin_ip=origin_ip,
            origin_port=origin_port,
            dest_ip=dest_ip,
            dest_port=dest_port,
            delivery_time_ms=delivery_time_ms
        ))

        # Keep packets sorted by delivery time
        self.pending_packets.sort(key=lambda p: p.delivery_time_ms)

        return True

    def receive(self, current_time_ms: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get all packets that should be delivered by the given time.

        Returns envelopes compatible with ReceiveFromNetworkHandler:
        - raw_data: bytes containing transit key ID + ciphertext
        - origin_ip: Source IP address
        - origin_port: Source port
        - received_at: Delivery timestamp

        Args:
            current_time_ms: Current time in milliseconds

        Returns:
            List of envelope dictionaries ready for the pipeline
        """
        if current_time_ms is not None:
            self.current_time_ms = current_time_ms

        ready_envelopes = []
        remaining_packets = []

        for packet in self.pending_packets:
            if packet.delivery_time_ms <= self.current_time_ms:
                # Create envelope compatible with ReceiveFromNetworkHandler
                envelope = {
                    'raw_data': packet.raw_data,
                    'origin_ip': packet.origin_ip,
                    'origin_port': packet.origin_port,
                    'received_at': packet.delivery_time_ms,
                    # Include destination for debugging/routing decisions
                    'dest_ip': packet.dest_ip,
                    'dest_port': packet.dest_port,
                }
                ready_envelopes.append(envelope)
            else:
                remaining_packets.append(packet)

        self.pending_packets = remaining_packets
        return ready_envelopes

    def advance_time(self, ms: int) -> List[Dict[str, Any]]:
        """
        Advance simulation time and return any packets now ready.

        Args:
            ms: Milliseconds to advance

        Returns:
            List of envelope dictionaries ready for delivery
        """
        self.current_time_ms += ms
        return self.receive(self.current_time_ms)

    def get_pending_count(self) -> int:
        """Get the number of packets waiting for delivery."""
        return len(self.pending_packets)

    def reset(self) -> None:
        """Reset the simulator state."""
        self.pending_packets.clear()
        self.current_time_ms = 0