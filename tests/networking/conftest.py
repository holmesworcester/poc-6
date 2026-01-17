"""Fixtures for real networking tests.

These tests use separate SQLite databases and real UDP sockets.
The transport callback is thread-local, so these tests CAN run in parallel.
Each test thread gets its own isolated callback.

Transport Architecture:
- Each Client has a UDPSocket for real network I/O
- Outgoing packets are intercepted via a transport callback and sent via UDP
- Incoming UDP packets are fed to sync.unwrap_and_store()
- Peer addresses are mapped via peer_shared_id -> (host, port)
"""

import pytest
import sqlite3
import socket
import threading
import queue
import time
import tempfile
import shutil
import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Callable

from core.db import Database, create_unsafe_db
from core import schema, tick as tick_module, queues
from events.identity import user, invite, peer
from events.content import channel, message

log = logging.getLogger('networking_tests')


# Mark all tests in this directory as networking tests
def pytest_collection_modifyitems(items):
    """Mark all networking tests."""
    for item in items:
        if "networking" in str(item.fspath):
            item.add_marker(pytest.mark.networking)


# =============================================================================
# UDP Socket Implementation
# =============================================================================

class UDPSocket:
    """Simple UDP socket for real network testing."""

    def __init__(self, port: int, host: str = '127.0.0.1'):
        self.port = port
        self.host = host
        self._sock = None
        self._incoming = queue.Queue()
        self._running = False
        self._recv_thread = None
        self._peers = {}  # peer_id -> (host, port)

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.1)
        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        log.debug(f"UDP socket listening on {self.host}:{self.port}")

    def stop(self):
        self._running = False
        if self._recv_thread:
            self._recv_thread.join(timeout=1.0)
        if self._sock:
            self._sock.close()

    def _recv_loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
                self._incoming.put((data, addr))
            except socket.timeout:
                continue
            except OSError:
                break

    def send(self, peer_id: str, data: bytes):
        """Send packet to a known peer."""
        if peer_id in self._peers:
            addr = self._peers[peer_id]
            self._sock.sendto(data, addr)

    def send_to(self, addr: tuple, data: bytes):
        """Send packet to an address directly."""
        self._sock.sendto(data, addr)

    # Alias for compatibility
    send_to_addr = send_to

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


# =============================================================================
# Client Classes
# =============================================================================

class RealClient:
    """A client with its own database and UDP socket.

    Used by: test_device_link.py, test_file_sync.py, test_three_player_messaging.py
    """

    def __init__(self, name: str, db_path: str, port: int):
        self.name = name
        self.db_path = db_path

        # Set up database
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self.db = Database(conn)
        schema.create_all(self.db)

        # Set up UDP
        self.network = UDPSocket(port)
        self.network.start()

        # Identity (set after new_network/join)
        self.peer_id = None
        self.peer_shared_id = None
        self.user_id = None
        self.network_id = None
        self.channel_id = None

        # Peer addresses for routing
        self.peer_addresses = {}

    def add_peer(self, peer_shared_id: str, host: str, port: int):
        """Register a peer's address for UDP routing."""
        self.peer_addresses[peer_shared_id] = (host, port)

    def receive_udp_packets(self, t_ms: int) -> int:
        """Move incoming UDP packets into SQLite queue."""
        packets = self.network.drain()
        if not packets:
            return 0

        unsafedb = create_unsafe_db(self.db)
        for data, addr in packets:
            queues.incoming.add_immediate(data, t_ms, unsafedb)
            log.debug(f"{self.name}: RECEIVED {len(data)}B UDP from {addr}")

        return len(packets)

    def tick(self, t_ms: int):
        """Run a tick: receive packets then process."""
        self.receive_udp_packets(t_ms)
        tick_module.tick(t_ms=t_ms, db=self.db)
        self.db.commit()

    def stop(self):
        self.network.stop()


@dataclass
class Client:
    """A test client with its own database and UDP socket.

    Named 'Client' not 'TestClient' to avoid pytest collection.
    Used by: test_sync_over_udp.py, test_udp_basic.py

    Transport Integration:
    - Incoming UDP packets are fed to sync.unwrap_and_store()
    - Outgoing packets are routed via UDP using the peer_addresses mapping
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
        """Move incoming UDP packets into this client's SQLite queue.

        Thread safety:
        - UDP recv thread puts packets into _incoming queue (thread-safe)
        - This method runs on main thread, INSERTs into SQLite
        - No SQLite access from background thread
        """
        packets = self.network.drain()
        if not packets:
            return 0

        # INSERT into this client's SQLite queue (main thread - safe)
        unsafedb = create_unsafe_db(self.db)
        for data, addr in packets:
            queues.incoming.add_immediate(data, t_ms, unsafedb)
            log.debug(f"Client {self.name}: queued UDP packet from {addr}")

        return len(packets)

    def send_to_peer(self, peer_shared_id: str, blob: bytes) -> bool:
        """Send a blob to a peer via UDP."""
        if peer_shared_id not in self.peer_addresses:
            log.warning(f"Client {self.name}: no address for peer {peer_shared_id[:20]}...")
            return False

        addr = self.peer_addresses[peer_shared_id]
        self.network.send_to_addr(addr, blob)
        log.debug(f"Client {self.name}: sent {len(blob)}B to {peer_shared_id[:20]}... at {addr}")
        return True

    def tick(self, t_ms: int):
        """Run a tick for this client, processing network packets."""
        # Step 1: Process incoming UDP packets through sync
        self.receive_udp_packets(t_ms)

        # Step 2: Run normal tick jobs (sync_receive, sync_send, etc.)
        tick_module.tick(t_ms=t_ms, db=self.db)
        self.db.commit()


# =============================================================================
# UDP Transport (for routing between Client instances)
# =============================================================================

class UDPTransport:
    """Transport layer that routes packets via UDP between clients.

    This enables real network communication between test clients by:
    1. Intercepting outgoing packets via the transport callback
    2. Looking up destination addresses by peer_shared_id
    3. Sending via UDP to the appropriate client

    Usage:
        transport = UDPTransport()
        transport.register_client(alice)
        transport.register_client(bob)
        transport.enable()  # Sets the global transport callback
        ...
        transport.disable()  # Clears the callback
    """

    def __init__(self):
        self.clients: dict[str, Client] = {}  # peer_shared_id -> Client
        self._enabled = False

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
        """Enable UDP routing by setting the transport callback."""
        queues.set_transport_callback(self._transport_callback)
        self._enabled = True
        log.info("UDPTransport: enabled")

    def disable(self):
        """Disable UDP routing by clearing the transport callback."""
        queues.set_transport_callback(None)
        self._enabled = False
        log.info("UDPTransport: disabled")

    def _transport_callback(self, blob: bytes, from_peer: str, to_peer: str, t_ms: int) -> bool:
        """Transport callback that routes packets via UDP."""
        # Find the destination client
        dest_client = self.clients.get(to_peer)
        if dest_client is None:
            # Unknown destination - let simulator handle it
            log.debug(f"UDPTransport: unknown dest {to_peer[:20] if to_peer else 'None'}..., using simulator")
            return False

        # Find the source client to send from
        source_client = None
        for client in self.clients.values():
            if client.peer_id == from_peer or client.peer_shared_id == from_peer:
                source_client = client
                break

        if source_client is None:
            log.debug(f"UDPTransport: unknown source {from_peer[:20] if from_peer else 'None'}..., using simulator")
            return False

        # Send via UDP
        dest_addr = (dest_client.network.host, dest_client.network.port)
        source_client.network.send_to_addr(dest_addr, blob)
        log.debug(f"UDPTransport: sent {len(blob)}B from {source_client.name} to {dest_client.name}")
        return True


# =============================================================================
# Helper Functions
# =============================================================================

def now_ms():
    """Get current wall-clock time in milliseconds."""
    return int(time.time() * 1000)


def tick_all(*clients: Client, t_ms: int):
    """Tick all clients at the same timestamp."""
    for client in clients:
        client.tick(t_ms)


def setup_transport_callback(clients: dict):
    """Set up transport callback to route packets via UDP.

    IMPORTANT: For real networking, the callback returns True for ALL packets,
    either routing them via UDP (if destination known) or dropping them (if not).
    This prevents packets from falling through to the simulator.
    """
    route_stats = {'routed': 0, 'unknown_dropped': 0, 'not_found_dropped': 0}

    def route_packet(blob: bytes, from_peer: str, to_peer: str, t_ms: int) -> bool:
        # Check for unknown/missing peer IDs
        if to_peer == "unknown" or from_peer == "unknown":
            route_stats['unknown_dropped'] += 1
            return True  # Return True to prevent simulator fallback

        # Find destination client
        for client in clients.values():
            if client.peer_shared_id == to_peer:
                # Find source client to send from
                for src_client in clients.values():
                    if src_client.peer_id == from_peer or src_client.peer_shared_id == from_peer:
                        dest_addr = (client.network.host, client.network.port)
                        src_client.network.send_to(dest_addr, blob)
                        route_stats['routed'] += 1
                        return True

        # Not routed - peer not found (drop the packet)
        route_stats['not_found_dropped'] += 1
        return True  # Return True to prevent simulator fallback

    queues.set_transport_callback(route_packet)
    return route_stats


def create_network_and_join(clients: dict, network_name: str = "TestNet"):
    """Helper to set up a network with all clients joined.

    Returns the channel_id for messaging.
    """
    alice = clients["alice"]
    t_ms = now_ms()

    # Alice creates a network
    result = user.new_network(name=network_name, t_ms=t_ms, db=alice.db)
    alice.peer_id = result['peer_id']
    alice.peer_shared_id = result['peer_shared_id']
    alice.user_id = result['user_id']
    alice.network_id = result['network_id']
    alice.channel_id = result['channel_id']
    alice.db.commit()

    # Alice creates an invite
    t_ms = now_ms()
    invite_id, invite_code, invite_data = invite.create(
        peer_id=alice.peer_id,
        t_ms=t_ms,
        db=alice.db
    )
    alice.db.commit()

    # Other clients join
    for name, client in clients.items():
        if name == "alice":
            continue

        t_ms = now_ms()

        # Create peer first
        client.peer_id = peer.create(t_ms=t_ms, db=client.db)
        client.db.commit()

        # Join the network
        join_result = user.join(
            peer_id=client.peer_id,
            invite_link=invite_code,
            name=name.capitalize(),
            device_name=f"{name.capitalize()}Device",
            t_ms=t_ms,
            db=client.db
        )
        client.peer_shared_id = join_result['peer_shared_id']
        client.user_id = join_result['user_id']
        client.network_id = join_result['network_id']
        client.channel_id = join_result['channel_id']
        client.db.commit()

    return alice.channel_id


def tick_until(clients: dict, check_fn, max_ticks: int = 100, tick_interval: float = 0.05):
    """Tick all clients until condition is met or timeout.

    Returns:
        Number of ticks taken, or None if timed out
    """
    for i in range(max_ticks):
        t_ms = now_ms()

        for client in clients.values():
            client.tick(t_ms)

        time.sleep(tick_interval)

        if check_fn():
            return i + 1

    return None


def assert_eventually_multi(
    check: Callable[[], bool],
    clients: list,
    start_t_ms: int,
    max_rounds: int = 100,
    interval_ms: int = 100,
    msg: str = None
) -> int:
    """Run ticks on all clients until check() passes.

    Like assert_eventually but for multiple clients with real networking.

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


# =============================================================================
# Port Allocation
# =============================================================================

_port_lock = threading.Lock()
# Use process ID to randomize base port (avoids conflicts between parallel test runs)
_port_base = 20000 + (os.getpid() % 1000) * 10
_port_offset = 0


def get_unique_ports(count: int) -> list[int]:
    """Get unique ports for a test (thread-safe).

    Port range: 20000-29999 (safely within valid range)
    """
    global _port_offset
    with _port_lock:
        start = _port_base + _port_offset
        _port_offset += count
        # Wrap around if we exceed valid range
        if start + count > 29999:
            _port_offset = count
            start = _port_base
        ports = list(range(start, start + count))
    return ports


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def tmp_dir():
    """Create a temporary directory for test databases."""
    tmp = tempfile.mkdtemp(prefix="networking_test_")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def two_clients(tmp_dir):
    """Create two clients (Alice and Bob) for networking tests."""
    ports = get_unique_ports(2)

    alice = RealClient("Alice", f"{tmp_dir}/alice.db", port=ports[0])
    bob = RealClient("Bob", f"{tmp_dir}/bob.db", port=ports[1])

    clients = {"alice": alice, "bob": bob}
    setup_transport_callback(clients)

    yield clients

    # Cleanup
    queues.set_transport_callback(None)
    alice.stop()
    bob.stop()


@pytest.fixture
def three_clients(tmp_dir):
    """Create three clients (Alice, Bob, Charlie) for networking tests."""
    ports = get_unique_ports(3)

    alice = RealClient("Alice", f"{tmp_dir}/alice.db", port=ports[0])
    bob = RealClient("Bob", f"{tmp_dir}/bob.db", port=ports[1])
    charlie = RealClient("Charlie", f"{tmp_dir}/charlie.db", port=ports[2])

    clients = {"alice": alice, "bob": bob, "charlie": charlie}
    setup_transport_callback(clients)

    yield clients

    # Cleanup
    queues.set_transport_callback(None)
    alice.stop()
    bob.stop()
    charlie.stop()


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
    base_port = 19000 + (os.getpid() % 10000)
    _next_port = [base_port]

    def _allocate():
        port = _next_port[0]
        _next_port[0] += 1
        return port

    return _allocate


@pytest.fixture
def create_client(fresh_client_db, udp_port_allocator):
    """Factory to create test clients (Client dataclass)."""
    clients = []

    def _create(name: str) -> Client:
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
