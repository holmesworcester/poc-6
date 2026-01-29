"""Simple address-based transport - in-memory queues for message passing.

Transport modes:
    - LOOPBACK: For testing, directly transfers outgoing to incoming (ignores to_addr)
    - SIMULATOR: For testing with network conditions (latency, packet loss, etc.)
    - UDP: For real network communication

The mode MUST be set explicitly. Sending without a configured mode is an error.
"""
from dataclasses import dataclass
from enum import Enum, auto
from threading import Lock
import time
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


# Simple pacing configuration (leaky bucket per destination).
@dataclass
class PacerConfig:
    rate_bytes_per_sec: int
    burst_bytes: int


DEFAULT_PACER_RATE_BYTES_PER_SEC = 2_000_000
DEFAULT_PACER_BURST_BYTES = 256_000


class _LeakyBucket:
    def __init__(self, config: PacerConfig) -> None:
        self.rate_bytes_per_sec = max(0, config.rate_bytes_per_sec)
        self.burst_bytes = max(0, config.burst_bytes)
        self.tokens = float(self.burst_bytes)
        self.last_ms: Optional[int] = None

    def allow(self, size_bytes: int, now_ms: int) -> bool:
        if self.rate_bytes_per_sec <= 0:
            return True
        if self.last_ms is None:
            self.last_ms = now_ms
        else:
            elapsed_ms = max(0, now_ms - self.last_ms)
            if elapsed_ms > 0:
                self.tokens = min(
                    float(self.burst_bytes),
                    self.tokens + (self.rate_bytes_per_sec * elapsed_ms) / 1000.0,
                )
                self.last_ms = now_ms

        if self.burst_bytes <= 0:
            return False

        if size_bytes > self.burst_bytes:
            # Allow oversized packets when we have any tokens.
            if self.tokens <= 0:
                return False
            self.tokens = 0.0
            return True

        if self.tokens >= size_bytes:
            self.tokens -= size_bytes
            return True
        return False


# Current mode
_mode: TransportMode = TransportMode.NONE

# In-memory queues
_incoming: list[tuple[bytes, tuple[str, int]]] = []  # (blob, from_addr)
_outgoing: list[tuple[bytes, tuple[str, int], tuple[str, int]]] = []  # (blob, from_addr, to_addr)
_lock = Lock()

# UDP networking state
_udp_socket: Optional['UDPSocket'] = None
_peer_addresses: dict[str, tuple[str, int]] = {}  # peer_shared_id -> (host, port)

# Simulator state
_simulator: Optional['NetworkSimulator'] = None
_simulator_time_ms: int = 0  # Current simulation time

# Pacer state (optional)
_pacer_config: Optional[PacerConfig] = None
_pacer_buckets: dict[tuple[str, int], _LeakyBucket] = {}


# ============================================================================
# Flow Control (credit-based, per flow_key)
# ============================================================================
# Separate from sync protocol - operates at transport level on blob receipts.

import os

@dataclass
class _FlowState:
    """Per-flow congestion control state."""
    window: int              # Max in-flight blobs
    in_flight: int = 0       # Currently awaiting ACK
    last_send_ms: int = 0    # Time of last send
    recv_pending: int = 0    # Received blobs pending ACK
    last_ack_ms: int = 0     # Time of last ACK sent


# Flow control parameters (tunable via env vars for testing)
FLOW_CONTROL_WINDOW = int(os.getenv("TRANSPORT_FLOW_WINDOW", "4096"))
FLOW_CONTROL_TIMEOUT_MS = int(os.getenv("TRANSPORT_FLOW_TIMEOUT_MS", "1000"))
FLOW_ACK_EVERY = int(os.getenv("TRANSPORT_ACK_EVERY", "100"))
FLOW_ACK_INTERVAL_MS = int(os.getenv("TRANSPORT_ACK_INTERVAL_MS", "200"))

_flow_state: dict[str, _FlowState] = {}


def _get_flow_state(flow_key: str) -> _FlowState:
    """Get or create flow state for a flow_key (typically connection key_id)."""
    state = _flow_state.get(flow_key)
    if not state:
        state = _FlowState(window=FLOW_CONTROL_WINDOW)
        _flow_state[flow_key] = state
    return state


def flow_can_send(flow_key: str, now_ms: int) -> bool:
    """Check if flow control allows sending on this flow."""
    if not flow_key:
        return True  # No flow control if no key
    state = _get_flow_state(flow_key)
    if state.in_flight < state.window:
        return True
    # Timeout - reset in_flight and allow
    if FLOW_CONTROL_TIMEOUT_MS > 0 and now_ms - state.last_send_ms >= FLOW_CONTROL_TIMEOUT_MS:
        state.in_flight = 0
        return True
    return False


def flow_on_send(flow_key: str, now_ms: int) -> None:
    """Record that we sent a blob on this flow."""
    if not flow_key:
        return
    state = _get_flow_state(flow_key)
    state.in_flight += 1
    state.last_send_ms = now_ms


def flow_on_ack(flow_key: str, count: int) -> None:
    """Record that we received an ACK for count blobs."""
    if not flow_key or count <= 0:
        return
    state = _get_flow_state(flow_key)
    state.in_flight = max(0, state.in_flight - count)
    log.debug(f"flow_on_ack: {flow_key[:16]}... count={count} in_flight={state.in_flight}")


def flow_on_receive(flow_key: str, now_ms: int) -> int:
    """Record blob receipt, return ACK count if threshold reached.

    Returns number of blobs to ACK (0 if not time to ACK yet).
    """
    if not flow_key:
        return 0
    state = _get_flow_state(flow_key)
    state.recv_pending += 1

    # ACK when we've received enough blobs or enough time has passed
    ack_threshold = min(FLOW_ACK_EVERY, max(1, state.window // 4))
    time_to_ack = FLOW_ACK_INTERVAL_MS > 0 and now_ms - state.last_ack_ms >= FLOW_ACK_INTERVAL_MS

    if state.recv_pending >= ack_threshold or (time_to_ack and state.recv_pending > 0):
        count = state.recv_pending
        state.recv_pending = 0
        state.last_ack_ms = now_ms
        return count
    return 0


def flow_reset(flow_key: str) -> None:
    """Reset flow state for a connection."""
    if flow_key in _flow_state:
        del _flow_state[flow_key]


def flow_reset_all() -> None:
    """Reset all flow state. For testing."""
    _flow_state.clear()


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
    if mode == TransportMode.LOOPBACK:
        _ensure_default_pacer()
    log.info(f"transport: mode set to {mode.name}")


def get_mode() -> TransportMode:
    """Get the current transport mode."""
    return _mode


def enable_loopback() -> None:
    """Convenience: enable loopback mode for testing."""
    set_mode(TransportMode.LOOPBACK)


def set_pacer(rate_bytes_per_sec: Optional[int], burst_bytes: Optional[int] = None) -> None:
    """Enable or disable send pacing.

    Args:
        rate_bytes_per_sec: Max bytes per second (None disables pacing)
        burst_bytes: Max burst size in bytes (defaults to rate_bytes_per_sec)
    """
    global _pacer_config, _pacer_buckets

    if rate_bytes_per_sec is None:
        _pacer_config = None
        _pacer_buckets.clear()
        log.info("transport: pacer disabled")
        return

    burst = burst_bytes if burst_bytes is not None else rate_bytes_per_sec
    _pacer_config = PacerConfig(rate_bytes_per_sec=rate_bytes_per_sec, burst_bytes=burst)
    _pacer_buckets.clear()
    log.info(f"transport: pacer enabled rate={rate_bytes_per_sec}B/s burst={burst}B")


def get_pacer_config() -> Optional[PacerConfig]:
    """Get current pacing configuration (if any)."""
    return _pacer_config


def _ensure_default_pacer() -> None:
    if _pacer_config is None:
        set_pacer(DEFAULT_PACER_RATE_BYTES_PER_SEC, DEFAULT_PACER_BURST_BYTES)


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
            "Transport not configured. Call enable_loopback(), start_udp(), or set_simulator() first."
        )

    # Only validate address for modes that need real routing
    # LOOPBACK and SIMULATOR ignore to_addr (all packets go to local incoming)
    if _mode == TransportMode.UDP:
        if not to_addr or not to_addr[0]:
            raise NoAddressError(
                f"Cannot send via UDP: no destination address. from_addr={from_addr}, to_addr={to_addr}"
            )

    with _lock:
        _outgoing.append((blob, from_addr, to_addr))
    return True


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


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _drain_outgoing_for_send(
    t_ms: Optional[int],
) -> list[tuple[bytes, tuple[str, int], tuple[str, int]]]:
    """Drain outgoing packets, applying pacing if enabled."""
    global _outgoing
    if _pacer_config is None:
        with _lock:
            batch = _outgoing[:]
            _outgoing.clear()
            return batch

    now_ms = _now_ms() if t_ms is None else t_ms
    with _lock:
        batch: list[tuple[bytes, tuple[str, int], tuple[str, int]]] = []
        remaining: list[tuple[bytes, tuple[str, int], tuple[str, int]]] = []
        for blob, from_addr, to_addr in _outgoing:
            key = to_addr or ('', 0)
            bucket = _pacer_buckets.get(key)
            if not bucket:
                bucket = _LeakyBucket(_pacer_config)
                _pacer_buckets[key] = bucket
            if bucket.allow(len(blob), now_ms):
                batch.append((blob, from_addr, to_addr))
            else:
                remaining.append((blob, from_addr, to_addr))
        _outgoing = remaining
        return batch


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
    else:
        raise TransportError(f"Unknown transport mode: {_mode}")


def _loopback_transfer() -> int:
    """Move all outgoing -> incoming directly (ignores to_addr)."""
    batch = _drain_outgoing_for_send(_simulator_time_ms if _simulator_time_ms else None)
    with _lock:
        for blob, from_addr, _ in batch:
            _incoming.append((blob, from_addr))
    return len(batch)


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
        _ensure_default_pacer()
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
    batch = _drain_outgoing_for_send(t_ms)
    for blob, from_addr, to_addr in batch:
        _simulator.add(blob, from_addr, to_addr, t_ms)

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
    _ensure_default_pacer()
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
    batch = _drain_outgoing_for_send(None)
    for blob, from_addr, to_addr in batch:
        _udp_socket.send_to(to_addr, blob)
        count += 1
        log.debug(f"transport: UDP sent {len(blob)}B to {to_addr}")

    with _lock:
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
# Reset
# ============================================================================

def reset():
    """Clear all queues, stop UDP, clear simulator, reset mode to NONE."""
    global _simulator, _simulator_time_ms, _udp_socket, _mode, _peer_addresses
    global _pacer_config, _pacer_buckets

    # Stop UDP if running
    if _udp_socket:
        _udp_socket.stop()
        _udp_socket = None

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
    _pacer_config = None
    _pacer_buckets.clear()

    log.info("transport: reset complete")
