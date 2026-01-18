"""Fixtures for real networking tests.

These tests use separate SQLite databases and real UDP sockets.

The transport callback is thread-local, so these tests CAN run in parallel.
Each test thread gets its own isolated callback.
"""

import pytest


# Mark all tests in this directory as networking tests
def pytest_collection_modifyitems(items):
    """Mark all networking tests."""
    for item in items:
        if "networking" in str(item.fspath):
            item.add_marker(pytest.mark.networking)
import sqlite3
import socket
import threading
import queue
import time
import tempfile
import shutil
import logging
import os

from core.db import Database, create_unsafe_db
from core import schema, tick as tick_module, queues
from events.identity import user, invite, peer
from events.content import channel, message

log = logging.getLogger('networking_tests')


class UDPSocket:
    """Simple UDP socket for real network testing."""

    def __init__(self, port: int, host: str = '127.0.0.1'):
        self.port = port
        self.host = host
        self._sock = None
        self._incoming = queue.Queue()
        self._running = False
        self._recv_thread = None

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

    def send_to(self, addr: tuple, data: bytes):
        self._sock.sendto(data, addr)

    def drain(self) -> list:
        packets = []
        while True:
            try:
                packets.append(self._incoming.get_nowait())
            except queue.Empty:
                break
        return packets


class RealClient:
    """A client with its own database and UDP socket."""

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

    def receive_udp_packets(self, t_ms: int) -> int:
        """Move incoming UDP packets into SQLite queue with source address info.

        Source addresses are passed through to enable address learning in the
        Connection layer.
        """
        packets = self.network.drain()
        if not packets:
            return 0

        unsafedb = create_unsafe_db(self.db)
        for data, addr in packets:
            source_ip, source_port = addr
            queues.incoming.add_immediate(data, t_ms, unsafedb,
                                          source_ip=source_ip, source_port=source_port)
            log.debug(f"{self.name}: RECEIVED {len(data)}B UDP from {source_ip}:{source_port}")

        return len(packets)

    def tick(self, t_ms: int):
        """Run a tick: receive packets then process."""
        self.receive_udp_packets(t_ms)
        tick_module.tick(t_ms=t_ms, db=self.db)
        self.db.commit()

    def stop(self):
        self.network.stop()


def setup_transport_callback(clients: dict):
    """Set up transport callback to route packets via UDP.

    IMPORTANT: For real networking, the callback uses the Connection layer to
    look up peer addresses from invite_accepteds (embedded in invite links).
    This is the "real" approach - no cheating by looking up client objects directly.
    """
    from events.network import connection_request as conn_module

    route_stats = {'routed': 0, 'unknown_dropped': 0, 'not_found_dropped': 0, 'no_address': 0}

    def route_packet(blob: bytes, from_peer: str, to_peer: str, t_ms: int) -> bool:
        # Check for unknown/missing peer IDs
        if to_peer == "unknown" or from_peer == "unknown":
            route_stats['unknown_dropped'] += 1
            return True  # Return True to prevent simulator fallback

        # Find source client to get their database and socket
        src_client = None
        for client in clients.values():
            if client.peer_id == from_peer or client.peer_shared_id == from_peer:
                src_client = client
                break

        if not src_client:
            route_stats['not_found_dropped'] += 1
            return True

        # Use Connection layer to look up destination address
        # This uses invite_accepteds (from invite link) or learned addresses
        dest_addr = conn_module.get_address_by_peer(to_peer, src_client.peer_id, src_client.db)

        if dest_addr:
            src_client.network.send_to(dest_addr, blob)
            route_stats['routed'] += 1
            log.debug(f"Transport: routed {len(blob)}B from {from_peer[:8]}... to {to_peer[:8]}... at {dest_addr}")
            return True

        # No address found - drop the packet
        route_stats['no_address'] += 1
        log.debug(f"Transport: no address for {to_peer[:8]}... from {from_peer[:8]}... - dropped")
        return True  # Return True to prevent simulator fallback

    queues.set_transport_callback(route_packet)
    return route_stats


def now_ms():
    """Get current wall-clock time in milliseconds."""
    return int(time.time() * 1000)


# Port allocation using thread-safe counter to avoid conflicts
import threading
import random
import os

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

    # Alice creates an invite with her network address embedded
    t_ms = now_ms()
    invite_id, invite_code, invite_data = invite.create(
        peer_id=alice.peer_id,
        t_ms=t_ms,
        db=alice.db,
        address=alice.network.host,
        port=alice.network.port
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


def tick_until(clients: dict, check_fn, max_ticks: int = 100, tick_interval: float = 0.05, debug: bool = False):
    """Tick all clients until condition is met or timeout.

    Args:
        clients: Dict of client name -> RealClient
        check_fn: Function that returns True when done
        max_ticks: Maximum number of tick rounds
        tick_interval: Seconds between ticks
        debug: If True, print route stats periodically

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
