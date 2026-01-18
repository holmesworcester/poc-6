"""Simple address-based transport - in-memory queues for message passing.

This replaces the complex NAT simulator with simple (ip, port) based routing.

Architecture:
    send() -> _outgoing -> [network layer] -> _incoming -> receive()

For testing, loopback_transfer() moves all _outgoing -> _incoming directly.
For production, udp_transfer() sends/receives via real UDP sockets.
"""
from threading import Lock
from typing import Any, Optional
import logging

log = logging.getLogger(__name__)

# In-memory queues
_incoming: list[tuple[bytes, tuple[str, int]]] = []  # (blob, from_addr)
_outgoing: list[tuple[bytes, tuple[str, int], tuple[str, int]]] = []  # (blob, from_addr, to_addr)
_lock = Lock()

# UDP networking state
_udp_socket: Optional['UDPSocket'] = None
_peer_addresses: dict[str, tuple[str, int]] = {}  # peer_shared_id -> (host, port)


def deliver(blob: bytes, from_addr: tuple[str, int]) -> None:
    """Deliver to incoming queue (called by UDP listener or loopback transfer).

    Args:
        blob: The packet data
        from_addr: Source address as (ip, port) tuple
    """
    with _lock:
        _incoming.append((blob, from_addr))


def drain_incoming(limit: int = 100) -> list[tuple[bytes, tuple[str, int]]]:
    """Grab batch from incoming queue.

    Args:
        limit: Maximum number of packets to return

    Returns:
        List of (blob, from_addr) tuples
    """
    with _lock:
        batch = _incoming[:limit]
        del _incoming[:limit]
        return batch


def send(blob: bytes, from_addr: tuple[str, int], to_addr: tuple[str, int]) -> bool:
    """Queue blob for sending (goes to outgoing buffer).

    Args:
        blob: The packet data
        from_addr: Source address as (ip, port) tuple
        to_addr: Destination address as (ip, port) tuple

    Returns:
        True (always succeeds for now)
    """
    with _lock:
        _outgoing.append((blob, from_addr, to_addr))
    return True


def drain_outgoing(limit: int = 100) -> list[tuple[bytes, tuple[str, int], tuple[str, int]]]:
    """Grab batch from outgoing queue (for tests/simulation to route).

    Args:
        limit: Maximum number of packets to return

    Returns:
        List of (blob, from_addr, to_addr) tuples
    """
    with _lock:
        batch = _outgoing[:limit]
        del _outgoing[:limit]
        return batch


def loopback_transfer() -> int:
    """Move all outgoing -> incoming (for testing). Returns count transferred.

    This is the simplest "network" - everything gets delivered immediately.
    """
    with _lock:
        count = len(_outgoing)
        for blob, from_addr, to_addr in _outgoing:
            _incoming.append((blob, from_addr))
        _outgoing.clear()
        return count


def reset():
    """Clear all queues."""
    with _lock:
        _incoming.clear()
        _outgoing.clear()


def pending_count() -> tuple[int, int]:
    """Get count of pending packets in incoming and outgoing queues.

    Returns:
        (incoming_count, outgoing_count)
    """
    with _lock:
        return len(_incoming), len(_outgoing)


# ============================================================================
# UDP Networking Functions
# ============================================================================

def start_udp(host: str, port: int) -> None:
    """Start UDP networking.

    Args:
        host: Host to bind to (e.g., '0.0.0.0')
        port: Port to listen on
    """
    global _udp_socket
    from core.udp import UDPSocket
    _udp_socket = UDPSocket(host, port)
    _udp_socket.start()
    log.info(f"transport: UDP started on {host}:{port}")


def stop_udp() -> None:
    """Stop UDP networking."""
    global _udp_socket
    if _udp_socket:
        _udp_socket.stop()
        _udp_socket = None
        log.info("transport: UDP stopped")


def add_peer_address(peer_shared_id: str, host: str, port: int) -> None:
    """Register a peer's network address.

    Args:
        peer_shared_id: Peer's shared ID
        host: Peer's IP address
        port: Peer's UDP port
    """
    _peer_addresses[peer_shared_id] = (host, port)
    log.info(f"transport: added peer {peer_shared_id[:20]}... at {host}:{port}")


def get_peer_address(peer_shared_id: str) -> Optional[tuple[str, int]]:
    """Look up a peer's network address.

    Args:
        peer_shared_id: Peer's shared ID

    Returns:
        (host, port) tuple or None if unknown
    """
    return _peer_addresses.get(peer_shared_id)


def udp_transfer() -> int:
    """Transfer: send outgoing via UDP, receive incoming from UDP.

    Called by ReceiveJob when UDP is active. Returns packets transferred.
    """
    global _udp_socket
    if not _udp_socket:
        return 0

    count = 0
    with _lock:
        # Send outgoing packets via UDP
        for blob, from_addr, to_addr in _outgoing:
            _udp_socket.send_to(to_addr, blob)
            count += 1
            log.debug(f"transport: UDP sent {len(blob)}B to {to_addr}")
        _outgoing.clear()

        # Receive incoming packets from UDP
        for data, addr in _udp_socket.drain():
            _incoming.append((data, addr))
            count += 1
            log.debug(f"transport: UDP received {len(data)}B from {addr}")

    return count


def is_udp_active() -> bool:
    """Check if UDP networking is active."""
    return _udp_socket is not None


def get_listen_address() -> Optional[tuple[str, int]]:
    """Get the local listen address if UDP is active.

    Returns:
        (host, port) tuple or None if UDP not active
    """
    if _udp_socket:
        return (_udp_socket.host, _udp_socket.port)
    return None
