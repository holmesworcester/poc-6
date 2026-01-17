#!/usr/bin/env python3
"""
Manual test: Two clients syncing over real UDP on localhost.

This tests the full stack:
1. Two separate SQLite databases
2. Real UDP sockets on localhost
3. Actual packet flow through the sync system
"""

import sqlite3
import socket
import threading
import queue
import time
import logging
import tempfile
import os
import sys

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(name)s: %(message)s')
log = logging.getLogger('test_real_udp')

# Suppress noisy modules
for name in ['events', 'crypto', 'store', 'tick', 'sync', 'queues', 'db']:
    logging.getLogger(name).setLevel(logging.WARNING)

from core.db import Database, create_unsafe_db
from core import schema, tick as tick_module, queues
from events.identity import user, invite, peer
from events.content import channel, message


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
        log.info(f"UDP socket listening on {self.host}:{self.port}")

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
            log.info(f"{self.name}: RECEIVED {len(data)}B UDP from {addr}")

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

    IMPORTANT: For real networking, the callback returns True for ALL packets,
    either routing them via UDP (if destination known) or dropping them (if not).
    This prevents packets from falling through to the simulator and accumulating
    in the local queue.
    """
    # Counter for logging frequency
    route_stats = {'routed': 0, 'unknown_dropped': 0, 'not_found_dropped': 0}

    def route_packet(blob: bytes, from_peer: str, to_peer: str, t_ms: int) -> bool:
        # Check for unknown/missing peer IDs (callback wasn't given routing info)
        if to_peer == "unknown" or from_peer == "unknown":
            route_stats['unknown_dropped'] += 1
            if route_stats['unknown_dropped'] <= 3:  # Only log first few
                log.warning(f"DROPPED (unknown peer): from={from_peer[:20]}..., to={to_peer[:20]}...")
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
                        if route_stats['routed'] <= 10:  # Log first few
                            log.info(f"ROUTED {len(blob)}B: {src_client.name} -> {client.name} (total: {route_stats['routed']})")
                        return True

        # Not routed - peer not found in clients (drop the packet)
        route_stats['not_found_dropped'] += 1
        if route_stats['not_found_dropped'] <= 3:  # Only log first few
            available_shared_ids = {name: c.peer_shared_id[:20] if c.peer_shared_id else None
                                    for name, c in clients.items()}
            log.warning(f"DROPPED (not found): from={from_peer[:20]}..., to={to_peer[:20]}...")
            log.warning(f"  Available peer_shared_ids: {available_shared_ids}")
        return True  # Return True to prevent simulator fallback

    queues.set_transport_callback(route_packet)


def main():
    print("=" * 60)
    print("Testing two clients syncing over REAL UDP")
    print("=" * 60)
    print()

    # Create temp directory for databases
    tmp_dir = tempfile.mkdtemp(prefix="real_udp_test_")

    try:
        # Create two clients
        alice = RealClient("Alice", f"{tmp_dir}/alice.db", port=19001)
        bob = RealClient("Bob", f"{tmp_dir}/bob.db", port=19002)

        clients = {"alice": alice, "bob": bob}

        # Set up transport callback for UDP routing
        setup_transport_callback(clients)

        # For real networking, use wall-clock time throughout
        def now_ms():
            return int(time.time() * 1000)

        t_ms = now_ms()

        # Alice creates a network (this also creates a main channel)
        print("[1] Alice creates a network...")
        result = user.new_network(name="TestNet", t_ms=t_ms, db=alice.db)
        alice.peer_id = result['peer_id']
        alice.peer_shared_id = result['peer_shared_id']
        alice.user_id = result['user_id']
        alice.network_id = result['network_id']
        alice.channel_id = result['channel_id']  # Main channel created automatically
        alice.db.commit()
        print(f"    Alice peer_shared_id: {alice.peer_shared_id[:20]}...")
        print(f"    Main channel ID: {alice.channel_id[:20]}...")

        t_ms = now_ms()

        # Alice creates an invite
        print("[2] Alice creates an invite...")
        invite_id, invite_code, invite_data = invite.create(
            peer_id=alice.peer_id,
            t_ms=t_ms,
            db=alice.db
        )
        alice.db.commit()
        print(f"    Invite code: {invite_code[:40]}...")

        t_ms = now_ms()

        # Bob joins using the invite
        print("[3] Bob joins using the invite...")
        # First Bob creates a peer
        bob.peer_id = peer.create(t_ms=t_ms, db=bob.db)
        bob.db.commit()

        # Then Bob joins
        join_result = user.join(
            peer_id=bob.peer_id,
            invite_link=invite_code,
            name="Bob",
            device_name="BobDevice",
            t_ms=t_ms,
            db=bob.db
        )
        bob.peer_shared_id = join_result['peer_shared_id']
        bob.user_id = join_result['user_id']
        bob.network_id = join_result['network_id']
        bob.channel_id = join_result['channel_id']  # Bob gets main channel from invite
        bob.db.commit()
        print(f"    Bob peer_shared_id: {bob.peer_shared_id[:20]}...")

        # Register peer addresses for UDP routing
        alice.add_peer(bob.peer_shared_id, bob.network.host, bob.network.port)
        bob.add_peer(alice.peer_shared_id, alice.network.host, alice.network.port)

        t_ms = now_ms()

        # Alice sends a message
        print("[4] Alice sends a message...")
        msg_result = message.create(
            content="Hello Bob via REAL UDP!",
            channel_id=alice.channel_id,
            peer_id=alice.peer_id,
            t_ms=t_ms,
            db=alice.db
        )
        msg_id = msg_result['id']
        alice.db.commit()
        print(f"    Message ID: {msg_id[:20]}...")

        # Run ticks to sync
        print("[5] Running sync ticks...")

        for i in range(50):  # Up to 50 ticks
            t_ms = now_ms()

            # Tick both clients
            alice.tick(t_ms)
            bob.tick(t_ms)

            # Small delay for UDP packets to fly
            time.sleep(0.05)

            # Check if Bob has the message
            bob_messages = message.list(
                channel_id=alice.channel_id,
                recorded_by=bob.peer_id,
                db=bob.db
            )

            if bob_messages:
                print(f"    Synced after {i+1} ticks!")
                break
        else:
            print("    WARNING: Did not sync after 50 ticks")

        # Verify Bob's view
        print()
        print("[6] Checking Bob's view...")
        bob_messages = message.list(
            channel_id=alice.channel_id,
            recorded_by=bob.peer_id,
            db=bob.db
        )

        if bob_messages:
            print(f"    Bob sees {len(bob_messages)} message(s)")
            for msg in bob_messages:
                print(f"      - \"{msg.get('content', msg.get('text', '?'))}\"")
        else:
            print("    Bob sees NO messages (sync may not be working)")

        print()
        print("=" * 60)
        print("Test complete!")
        print("=" * 60)

    finally:
        # Cleanup
        queues.set_transport_callback(None)
        alice.stop()
        bob.stop()

        # Clean up temp files
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
