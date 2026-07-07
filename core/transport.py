"""Simple address-based transport - in-memory queues for message passing.

Transport modes:
    - LOOPBACK: For testing, directly transfers outgoing to incoming (ignores to_addr)
    - SIMULATOR: For testing with network conditions (latency, packet loss, etc.)
    - UDP: For real network communication

The mode MUST be set explicitly. Sending without a configured mode is an error.
"""
from enum import Enum, auto
from threading import Lock
from typing import Any, Optional, TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from core.simulator import NetworkSimulator

log = logging.getLogger(__name__)


class TransportMode(Enum):
    """Transport mode - must be set explicitly."""
    NONE = auto()      # Not configured - send() will error
    LOOPBACK = auto()  # Testing: outgoing -> incoming directly
    SIMULATOR = auto() # Testing: outgoing -> simulator -> incoming
    UDP = auto()       # Production: real UDP sockets
    QUIC = auto()      # Production: QUIC relay transport


# Current mode
_mode: TransportMode = TransportMode.NONE

# In-memory queues
_incoming: list[tuple[bytes, tuple[str, int]]] = []  # (blob, from_addr)
_outgoing: list[tuple[bytes, tuple[str, int], tuple[str, int]]] = []  # (blob, from_addr, to_addr)
_lock = Lock()

# UDP networking state
_udp_socket: Optional['UDPSocket'] = None
_peer_addresses: dict[str, tuple[str, int]] = {}  # peer_shared_id -> (host, port)
_quic_client: Optional['QuicRelayClient'] = None

# Simulator state
_simulator: Optional['NetworkSimulator'] = None
_simulator_time_ms: int = 0  # Current simulation time


class TransportError(Exception):
    """Error for transport operations."""
    pass


class NoAddressError(TransportError):
    """Error when trying to send without a destination address."""
    pass


class TransportNotConfiguredError(TransportError):
    """Error when trying to use transport without setting a mode."""
    pass


# ============================================================================
# Mode Configuration
# ============================================================================

def set_mode(mode: TransportMode) -> None:
    """Set the transport mode.

    Args:
        mode: The transport mode to use

    Raises:
        TransportError: If UDP mode requested but UDP not started
    """
    global _mode

    if mode == TransportMode.UDP and not _udp_socket:
        raise TransportError("Cannot set UDP mode: UDP socket not started. Call start_udp() first.")

    if mode == TransportMode.SIMULATOR and not _simulator:
        raise TransportError("Cannot set SIMULATOR mode: No simulator set. Call set_simulator() first.")

    _mode = mode
    log.info(f"transport: mode set to {mode.name}")


def get_mode() -> TransportMode:
    """Get the current transport mode."""
    return _mode


def enable_loopback() -> None:
    """Convenience: enable loopback mode for testing."""
    set_mode(TransportMode.LOOPBACK)


# ============================================================================
# Core Queue Operations
# ============================================================================

def send(blob: bytes, from_addr: tuple[str, int], to_addr: tuple[str, int]) -> bool:
    """Queue blob for sending.

    Args:
        blob: The packet data
        from_addr: Source address as (ip, port) tuple
        to_addr: Destination address as (ip, port) tuple (ignored in loopback mode)

    Returns:
        True on success

    Raises:
        TransportNotConfiguredError: If no transport mode is set
        NoAddressError: If to_addr is None/invalid AND mode requires addresses (UDP)
    """
    if _mode == TransportMode.NONE:
        raise TransportNotConfiguredError(
            "Transport not configured. Call enable_loopback(), start_udp(), start_quic_relay(), or set_simulator() first."
        )

    # Only validate address for modes that need real routing
    # LOOPBACK and SIMULATOR ignore to_addr (all packets go to local incoming)
    if _mode == TransportMode.UDP:
        if not to_addr or not to_addr[0]:
            raise NoAddressError(
                f"Cannot send via UDP: no destination address. from_addr={from_addr}, to_addr={to_addr}"
            )
    if _mode == TransportMode.QUIC:
        raise TransportError("Use send_to_peer() for QUIC relay transport.")

    with _lock:
        _outgoing.append((blob, from_addr, to_addr))
    return True


def send_to_peer(peer_shared_id: str, blob: bytes, from_addr: tuple[str, int] | None = None,
                 to_addr: tuple[str, int] | None = None) -> bool:
    """Send a blob to a peer, routing via address or QUIC relay as needed."""
    if _mode == TransportMode.QUIC:
        if not _quic_client:
            raise TransportError("QUIC relay not started. Call start_quic_relay() first.")
        _quic_client.send(peer_shared_id, blob)
        return True

    if not to_addr:
        to_addr = get_peer_address(peer_shared_id)
    if not to_addr:
        raise NoAddressError(f"Cannot send: no destination for peer {peer_shared_id[:20]}...")
    if not from_addr:
        from_addr = get_listen_address() or ('127.0.0.1', 0)
    return send(blob, from_addr, to_addr)


def deliver(blob: bytes, from_addr: tuple[str, int]) -> None:
    """Deliver directly to incoming queue.

    This should only be used:
    - By UDP receiver thread when packets arrive
    - By loopback_transfer() when moving outgoing to incoming
    - In tests that want to inject packets

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


def pending_count() -> tuple[int, int]:
    """Get count of pending packets in incoming and outgoing queues.

    Returns:
        (incoming_count, outgoing_count)
    """
    with _lock:
        return len(_incoming), len(_outgoing)


# ============================================================================
# Transfer Functions (move outgoing -> incoming based on mode)
# ============================================================================

def transfer() -> int:
    """Transfer packets based on current mode.

    - LOOPBACK: Direct transfer outgoing -> incoming
    - SIMULATOR: Process through simulator with latency/loss
    - UDP: Send via real UDP, receive from UDP

    Returns:
        Number of packets transferred

    Raises:
        TransportNotConfiguredError: If no mode is set
    """
    if _mode == TransportMode.NONE:
        raise TransportNotConfiguredError(
            "Transport not configured. Call enable_loopback(), start_udp(), or set_simulator() first."
        )

    if _mode == TransportMode.LOOPBACK:
        return _loopback_transfer()
    elif _mode == TransportMode.SIMULATOR:
        return _simulator_transfer(_simulator_time_ms)
    elif _mode == TransportMode.UDP:
        return _udp_transfer()
    elif _mode == TransportMode.QUIC:
        return _quic_transfer()
    else:
        raise TransportError(f"Unknown transport mode: {_mode}")


def _loopback_transfer() -> int:
    """Move all outgoing -> incoming directly (ignores to_addr)."""
    with _lock:
        count = len(_outgoing)
        for blob, from_addr, to_addr in _outgoing:
            _incoming.append((blob, from_addr))
        _outgoing.clear()
        return count


# Legacy alias for tests that call loopback_transfer() directly
def loopback_transfer() -> int:
    """Legacy: Move all outgoing -> incoming.

    If simulator is set, uses simulator. Otherwise direct transfer.
    Prefer using transfer() instead.
    """
    if _simulator is not None:
        return _simulator_transfer(_simulator_time_ms)
    return _loopback_transfer()


# ============================================================================
# Simulator Functions
# ============================================================================

def set_simulator(sim: Optional['NetworkSimulator']) -> None:
    """Set the network simulator for testing network conditions.

    Also sets mode to SIMULATOR if sim is not None.

    Args:
        sim: NetworkSimulator instance, or None to disable
    """
    global _simulator, _mode
    _simulator = sim
    if sim:
        _mode = TransportMode.SIMULATOR
        log.info("transport: simulator enabled, mode set to SIMULATOR")
    else:
        if _mode == TransportMode.SIMULATOR:
            _mode = TransportMode.NONE
        log.info("transport: simulator disabled")


def get_simulator() -> Optional['NetworkSimulator']:
    """Get the current simulator instance."""
    return _simulator


def is_simulator_active() -> bool:
    """Check if simulator is enabled."""
    return _simulator is not None


def set_simulator_time(t_ms: int) -> None:
    """Set the current simulation time.

    This should be called before transfer() to ensure
    the simulator knows the current time for latency calculations.
    """
    global _simulator_time_ms
    _simulator_time_ms = t_ms


def _simulator_transfer(t_ms: int) -> int:
    """Process packets through the simulator."""
    global _simulator_time_ms
    _simulator_time_ms = t_ms

    if not _simulator:
        return _loopback_transfer()

    # Move outgoing -> simulator
    with _lock:
        for blob, from_addr, to_addr in _outgoing:
            _simulator.add(blob, from_addr, to_addr, t_ms)
        _outgoing.clear()

    # Drain ready packets from simulator -> incoming
    ready = _simulator.drain(t_ms)
    with _lock:
        for blob, from_addr in ready:
            _incoming.append((blob, from_addr))

    return len(ready)


# Legacy alias
def simulator_transfer(t_ms: int) -> int:
    """Legacy: Process packets through simulator. Prefer transfer()."""
    return _simulator_transfer(t_ms)


# ============================================================================
# UDP Networking Functions
# ============================================================================

def start_udp(host: str, port: int) -> None:
    """Start UDP networking and set mode to UDP.

    Args:
        host: Host to bind to (e.g., '0.0.0.0')
        port: Port to listen on
    """
    global _udp_socket, _mode
    from core.udp import UDPSocket
    _udp_socket = UDPSocket(host, port)
    _udp_socket.start()
    _mode = TransportMode.UDP
    log.info(f"transport: UDP started on {host}:{port}, mode set to UDP")


def stop_udp() -> None:
    """Stop UDP networking."""
    global _udp_socket, _mode
    if _udp_socket:
        _udp_socket.stop()
        _udp_socket = None
        if _mode == TransportMode.UDP:
            _mode = TransportMode.NONE
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


def _udp_transfer() -> int:
    """Transfer: send outgoing via UDP, receive incoming from UDP."""
    global _udp_socket
    if not _udp_socket:
        raise TransportError("UDP transfer called but UDP socket not started")

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


# Legacy aliases
def udp_transfer() -> int:
    """Legacy: Transfer via UDP. Prefer transfer()."""
    return _udp_transfer()


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


# ============================================================================
# QUIC Networking Functions
# ============================================================================

def start_quic_relay(relay_url: str, peer_shared_id: str, insecure: bool = False) -> None:
    """Start QUIC relay client and set mode to QUIC."""
    global _quic_client, _mode
    from core.quic import QuicRelayClient
    _quic_client = QuicRelayClient(relay_url, peer_shared_id, insecure=insecure)
    _quic_client.start()
    _mode = TransportMode.QUIC
    log.info(f"transport: QUIC relay started at {relay_url}, mode set to QUIC")


def stop_quic_relay() -> None:
    """Stop QUIC relay client."""
    global _quic_client, _mode
    if _quic_client:
        _quic_client.stop()
        _quic_client = None
        if _mode == TransportMode.QUIC:
            _mode = TransportMode.NONE
        log.info("transport: QUIC relay stopped")


def is_quic_active() -> bool:
    """Check if QUIC relay client is active."""
    return _quic_client is not None


def _quic_transfer() -> int:
    """Transfer: receive incoming from QUIC relay."""
    if not _quic_client:
        raise TransportError("QUIC transfer called but QUIC relay not started")
    count = 0
    relay_addr = _quic_client.relay_addr
    with _lock:
        for blob in _quic_client.drain():
            _incoming.append((blob, relay_addr))
            count += 1
    return count


# ============================================================================
# Reset
# ============================================================================

def reset():
    """Clear all queues, stop UDP, clear simulator, reset mode to NONE."""
    global _simulator, _simulator_time_ms, _udp_socket, _mode, _peer_addresses, _quic_client

    # Stop UDP if running
    if _udp_socket:
        _udp_socket.stop()
        _udp_socket = None

    # Stop QUIC if running
    if _quic_client:
        _quic_client.stop()
        _quic_client = None

    # Clear queues
    with _lock:
        _incoming.clear()
        _outgoing.clear()

    # Reset simulator
    if _simulator:
        _simulator.reset()
    _simulator = None
    _simulator_time_ms = 0

    # Clear peer addresses
    _peer_addresses.clear()

    # Reset mode
    _mode = TransportMode.NONE

    log.info("transport: reset complete")
