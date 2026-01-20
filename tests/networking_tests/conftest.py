"""
Fixtures for networking tests.

Networking tests use REAL UDP sockets on localhost to test actual packet flow
between multiple CLI clients. Each client has its own database.

Key differences from scenario tests:
- Multiple separate databases (not peer-scoped views of one db)
- Real UDP sockets on different ports
- Packets actually flow over localhost
- Uses assert_eventually pattern for sync verification

Transport Architecture:
- Each Client has a UDPSocket for real network I/O
- Uses core.transport module for packet routing
- Incoming UDP packets are fed to transport.deliver()
- Peer addresses are mapped via peer_shared_id -> (host, port)
"""
import pytest
import socket
import sqlite3
import threading
import queue
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Any
from core.db import Database
from core import schema

log = logging.getLogger(__name__)


@dataclass
class UDPSocket:
    """Simple UDP socket wrapper for testing.

    This is a minimal implementation for tests - the real one is in core/udp.py
    """
    port: int
    host: str = '127.0.0.1'

    # Internal state
    _sock: socket.socket = field(default=None, repr=False)
    _incoming: queue.Queue = field(default_factory=queue.Queue, repr=False)
    _running: bool = field(default=False, repr=False)
    _recv_thread: threading.Thread = field(default=None, repr=False)
    _peers: dict = field(default_factory=dict, repr=False)  # peer_id -> (host, port)

    def start(self):
        """Start listening for UDP packets."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.1)  # Non-blocking with timeout
        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def stop(self):
        """Stop the network."""
        self._running = False
        if self._recv_thread:
            self._recv_thread.join(timeout=1.0)
        if self._sock:
            self._sock.close()

    def _recv_loop(self):
        """Background thread: receive packets and queue them."""
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
                self._incoming.put((data, addr))
            except socket.timeout:
                continue
            except OSError:
                break  # Socket closed

    def send(self, peer_id: str, data: bytes):
        """Send packet to a known peer."""
        if peer_id in self._peers:
            addr = self._peers[peer_id]
            self._sock.sendto(data, addr)

    def send_to_addr(self, addr: tuple, data: bytes):
        """Send packet to an address directly."""
        self._sock.sendto(data, addr)

    def add_peer(self, peer_id: str, addr: tuple):
        """Register a peer's address."""
        self._peers[peer_id] = addr

    def drain(self) -> list:
        """Drain all queued packets."""
        packets = []
        while True:
            try:
                packets.append(self._incoming.get_nowait())
            except queue.Empty:
                break
        return packets

    def has_packets(self) -> bool:
        """Check if there are packets waiting."""
        return not self._incoming.empty()


@dataclass
class Client:
    """A test client with its own database and UDP socket.

    Named 'Client' not 'TestClient' to avoid pytest collection.

    Transport Integration:
    - Incoming UDP packets are fed to transport.deliver()
    - Outgoing packets are routed via transport.send()
    - peer_addresses maps peer_shared_id -> (host, port)
    """
    name: str
    db: Database
    network: UDPSocket
    peer_id: Optional[str] = None
    peer_shared_id: Optional[str] = None
    user_id: Optional[str] = None
    network_id: Optional[str] = None
    channel_id: Optional[str] = None

    # Mapping from peer_shared_id -> (host, port) for real networking
    peer_addresses: dict = field(default_factory=dict)

    # Reference to other clients for routing (set by test)
    _client_registry: dict = field(default_factory=dict, repr=False)

    def add_peer_address(self, peer_shared_id: str, host: str, port: int):
        """Register a peer's network address for real UDP transport."""
        self.peer_addresses[peer_shared_id] = (host, port)
        # Also add to the UDPSocket's peer map for convenience
        self.network.add_peer(peer_shared_id, (host, port))

    def receive_udp_packets(self, t_ms: int) -> int:
        """Move incoming UDP packets into the transport incoming queue.

        Drains the UDP socket's thread-safe buffer and calls transport.deliver()
        for each packet. The packets will be processed by ReceiveJob during tick().

        Thread safety:
        - UDP recv thread puts packets into _incoming queue (thread-safe)
        - This method runs on main thread
        - No SQLite access from background thread

        Args:
            t_ms: Current timestamp for event recording

        Returns:
            Number of packets queued
        """
        from core import transport

        packets = self.network.drain()
        if not packets:
            return 0

        for data, addr in packets:
            transport.deliver(data, addr)
            log.debug(f"Client {self.name}: delivered UDP packet from {addr}")

        return len(packets)

    def send_to_peer(self, peer_shared_id: str, blob: bytes) -> bool:
        """Send a blob to a peer via UDP.

        Args:
            peer_shared_id: Destination peer's shared ID
            blob: Transit-wrapped blob to send

        Returns:
            True if sent, False if peer address unknown
        """
        if peer_shared_id not in self.peer_addresses:
            log.warning(f"Client {self.name}: no address for peer {peer_shared_id[:20]}...")
            return False

        addr = self.peer_addresses[peer_shared_id]
        self.network.send_to_addr(addr, blob)
        log.debug(f"Client {self.name}: sent {len(blob)}B to {peer_shared_id[:20]}... at {addr}")
        return True

    def tick(self, t_ms: int):
        """Run a tick for this client, processing network packets.

        This integrates UDP with the sync system:
        1. First, receive and process any incoming UDP packets
        2. Then run the normal tick jobs
        """
        from core import tick as tick_module

        # Step 1: Process incoming UDP packets through transport
        self.receive_udp_packets(t_ms)

        # Step 2: Run normal tick jobs (ReceiveJob will drain transport.incoming)
        tick_module.tick(t_ms=t_ms, db=self.db)
        self.db.commit()


@pytest.fixture
def fresh_client_db(tmp_path_factory):
    """Create a fresh disk-based database for a client.

    Uses real disk with WAL mode, like scenario tests.
    Each call creates a new isolated database.
    """
    created_dbs = []

    def _create():
        # Create unique path for this client's DB
        db_dir = tmp_path_factory.mktemp("client_db")
        db_path = db_dir / "client.db"
        conn = sqlite3.connect(str(db_path))
        db = Database(conn)

        # Performance optimizations (WAL is enabled in Database.__init__)
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
        conn.execute("PRAGMA temp_store = MEMORY")

        schema.create_all(db)
        created_dbs.append((conn, db_path))
        return db

    yield _create

    # Cleanup
    for conn, path in created_dbs:
        conn.close()


@pytest.fixture
def udp_port_allocator():
    """Allocate unique UDP ports for tests.

    Uses process ID to avoid conflicts when running tests in parallel.
    """
    import os
    base_port = 19000 + (os.getpid() % 10000)
    _next_port = [base_port]

    def _allocate():
        port = _next_port[0]
        _next_port[0] += 1
        return port

    return _allocate


@pytest.fixture
def create_client(fresh_client_db, udp_port_allocator):
    """Factory to create test clients."""
    from core import transport

    clients = []

    def _create(name: str) -> Client:
        # Reset transport state for each test
        transport.reset()

        db = fresh_client_db()
        port = udp_port_allocator()
        network = UDPSocket(port=port)
        network.start()
        client = Client(name=name, db=db, network=network)
        clients.append(client)
        return client

    yield _create

    # Cleanup
    for client in clients:
        client.network.stop()

    # Reset transport
    transport.reset()


def tick_all(*clients: Client, t_ms: int):
    """Tick all clients at the same timestamp."""
    for client in clients:
        client.tick(t_ms)


class UDPTransport:
    """Transport layer that routes packets via UDP between clients.

    This enables real network communication between test clients by:
    1. Intercepting outgoing packets from transport._outgoing
    2. Looking up destination addresses by peer_shared_id
    3. Sending via UDP to the appropriate client

    Usage:
        transport = UDPTransport()
        transport.register_client(alice)
        transport.register_client(bob)
        transport.enable()  # Start routing
        ...
        transport.disable()  # Stop routing
    """

    def __init__(self):
        self.clients: dict[str, Client] = {}  # peer_shared_id -> Client
        self._enabled = False
        self._routing_thread: Optional[threading.Thread] = None
        self._running = False

    def register_client(self, client: Client):
        """Register a client for UDP routing.

        The client must have peer_shared_id set.
        """
        if not client.peer_shared_id:
            log.warning(f"UDPTransport: client {client.name} has no peer_shared_id")
            return

        self.clients[client.peer_shared_id] = client
        log.info(f"UDPTransport: registered {client.name} as {client.peer_shared_id[:20]}...")

        # Add all other clients' addresses to this client
        for other_id, other_client in self.clients.items():
            if other_id != client.peer_shared_id:
                # Add other's address to this client
                client.add_peer_address(other_id, other_client.network.host, other_client.network.port)
                # Add this client's address to other
                other_client.add_peer_address(client.peer_shared_id, client.network.host, client.network.port)

    def enable(self):
        """Enable UDP routing."""
        self._enabled = True
        log.info("UDPTransport: enabled")

    def disable(self):
        """Disable UDP routing."""
        self._enabled = False
        log.info("UDPTransport: disabled")

    def route_outgoing(self):
        """Route any pending outgoing packets via UDP.

        Should be called periodically or before checking received packets.
        """
        from core import transport

        outgoing = transport.drain_outgoing(100)
        for blob, from_addr, to_addr in outgoing:
            # Find the source client
            source_client = None
            for client in self.clients.values():
                if (client.network.host, client.network.port) == from_addr:
                    source_client = client
                    break

            if source_client:
                # Send via UDP
                source_client.network.send_to_addr(to_addr, blob)
                log.debug(f"UDPTransport: routed {len(blob)}B to {to_addr}")


@pytest.fixture
def udp_transport():
    """Create a UDP transport for routing between clients.

    The transport must be enabled after clients are registered:
        transport.register_client(alice)
        transport.register_client(bob)
        transport.enable()
    """
    transport = UDPTransport()
    yield transport
    transport.disable()  # Cleanup


def assert_eventually_multi(
    check: Callable[[], bool],
    clients: list[Client],
    start_t_ms: int,
    max_rounds: int = 100,
    interval_ms: int = 100,
    msg: str = None
) -> int:
    """Run ticks on all clients until check() passes.

    Like assert_eventually but for multiple clients with real networking.

    Args:
        check: Function that returns True when condition is met
        clients: List of Client instances to tick
        start_t_ms: Starting timestamp
        max_rounds: Maximum ticks before timeout
        interval_ms: Time between ticks
        msg: Optional failure message

    Returns:
        Final timestamp
    """
    for i in range(max_rounds):
        t_ms = start_t_ms + i * interval_ms

        # Tick all clients
        for client in clients:
            client.tick(t_ms)

        # Small sleep to let UDP packets fly
        time.sleep(0.001)

        # Check condition
        try:
            if check():
                return t_ms + interval_ms
        except Exception:
            pass  # Not ready yet

    # Final check with real error
    timeout_msg = msg or f"Condition not met after {max_rounds} rounds"
    try:
        result = check()
        if not result:
            raise AssertionError(timeout_msg)
    except Exception as e:
        raise AssertionError(f"{timeout_msg}: {e}") from None

    return start_t_ms + max_rounds * interval_ms
