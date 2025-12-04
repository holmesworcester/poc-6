"""Stubbed connection interface for isolated sync testing.

Bypasses the real connection layer (encryption, handshakes, prekeys) to allow
direct testing of sync protocols. Messages are routed in-memory between peers.

Key features:
- No encryption/decryption overhead
- No handshake dance required
- Messages logged for visibility
- Supports simulating delays, losses, reordering
"""

import sqlite3
import logging
from dataclasses import dataclass, field
from typing import Callable
from collections import deque

from db import Database, create_safe_db
import schema
import crypto

log = logging.getLogger(__name__)


@dataclass
class StubMessage:
    """A message in the stub network."""
    from_peer_id: str
    to_peer_id: str
    connection_id: str  # Sender's connection_id
    reply_connection_id: str  # Receiver's connection_id
    blob: bytes
    sent_at_ms: int
    delivered: bool = False


@dataclass
class StubConnection:
    """A stub connection between two peers.

    Unlike real connections which require handshakes and symmetric keys,
    stub connections are immediately bidirectional.
    """
    connection_id: str  # Our connection_id
    local_peer_id: str
    remote_peer_id: str
    remote_peer_shared_id: str
    their_connection_id: str  # Their connection_id (for routing)
    created_at: int

    def can_send(self) -> bool:
        return True  # Always ready

    def is_bidirectional(self) -> bool:
        return True  # Always bidirectional


@dataclass
class StubPeer:
    """A peer in the stub network with its own database."""
    peer_id: str
    peer_shared_id: str
    db: Database
    name: str = ""
    connections: dict[str, StubConnection] = field(default_factory=dict)

    @classmethod
    def create(cls, name: str, t_ms: int = 1000) -> 'StubPeer':
        """Create a new stub peer with fresh database."""
        conn = sqlite3.Connection(":memory:")
        db = Database(conn)
        schema.create_all(db)

        # Generate peer identity (use deterministic but unique IDs)
        peer_id = f"peer_{name.lower()}_{t_ms}"
        peer_shared_id = f"peer_shared_{name.lower()}_{t_ms}"

        # Generate placeholder keys (real keys not needed for stub testing)
        # Just need something to satisfy NOT NULL constraints
        stub_public_key = f"pubkey_{name.lower()}_{t_ms}"
        stub_private_key = f"privkey_{name.lower()}_{t_ms}".encode()

        # Mark this as a local peer with all required fields
        db.execute(
            "INSERT INTO local_peers (peer_id, public_key, private_key, created_at) VALUES (?, ?, ?, ?)",
            (peer_id, stub_public_key, stub_private_key, t_ms)
        )

        db.commit()

        return cls(
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            db=db,
            name=name,
        )


class StubConnectionBridge:
    """Bridge that routes messages between stub peers.

    Replaces the real connection.send() with direct routing.
    Provides visibility into all message traffic.

    Usage:
        bridge = StubConnectionBridge()
        alice = bridge.add_peer("Alice")
        bob = bridge.add_peer("Bob")
        bridge.connect(alice, bob)

        # Now sync protocols can use connection.send()
        # Messages are routed through the bridge
    """

    def __init__(self, verbose: bool = True):
        self.peers: dict[str, StubPeer] = {}  # peer_id -> StubPeer
        self.peer_shared_to_id: dict[str, str] = {}  # peer_shared_id -> peer_id
        self.message_queue: deque[StubMessage] = deque()
        self.delivered_messages: list[StubMessage] = []
        self.verbose = verbose
        self._original_send = None
        self._installed = False

    def add_peer(self, name: str, t_ms: int = 1000) -> StubPeer:
        """Create and register a new peer."""
        peer = StubPeer.create(name, t_ms)
        self.peers[peer.peer_id] = peer
        self.peer_shared_to_id[peer.peer_shared_id] = peer.peer_id

        if self.verbose:
            log.info(f"[STUB] Added peer {name}: {peer.peer_id[:20]}...")

        return peer

    def connect(self, peer_a: StubPeer, peer_b: StubPeer, t_ms: int = 2000) -> None:
        """Create bidirectional stub connection between two peers.

        Unlike real connections, this is immediate and doesn't require handshakes.
        """
        # Create connection IDs (normally these would be event IDs)
        conn_a_to_b = f"conn_{peer_a.name}_to_{peer_b.name}_{t_ms}"
        conn_b_to_a = f"conn_{peer_b.name}_to_{peer_a.name}_{t_ms}"

        # A's view of connection to B
        peer_a.connections[conn_a_to_b] = StubConnection(
            connection_id=conn_a_to_b,
            local_peer_id=peer_a.peer_id,
            remote_peer_id=peer_b.peer_id,
            remote_peer_shared_id=peer_b.peer_shared_id,
            their_connection_id=conn_b_to_a,
            created_at=t_ms,
        )

        # B's view of connection to A
        peer_b.connections[conn_b_to_a] = StubConnection(
            connection_id=conn_b_to_a,
            local_peer_id=peer_b.peer_id,
            remote_peer_id=peer_a.peer_id,
            remote_peer_shared_id=peer_a.peer_shared_id,
            their_connection_id=conn_a_to_b,
            created_at=t_ms,
        )

        # Store connection info in databases for get_connections() calls
        self._store_connection(peer_a, peer_a.connections[conn_a_to_b])
        self._store_connection(peer_b, peer_b.connections[conn_b_to_a])

        if self.verbose:
            log.info(f"[STUB] Connected {peer_a.name} <-> {peer_b.name}")

    def _store_connection(self, peer: StubPeer, conn: StubConnection) -> None:
        """Store stub connection in peer's database for lookup."""
        safedb = create_safe_db(peer.db, recorded_by=peer.peer_id)

        # Use placeholder key - we don't need real crypto for stub connections
        placeholder_key = b'stubkey_' + conn.connection_id[:24].encode()

        safedb.execute("""
            INSERT OR REPLACE INTO connections (
                connection_id, recorded_by, peer_shared_id, invite_id,
                our_key, their_connection_id, their_key,
                created_at, last_handshake_ms, ttl_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            conn.connection_id,
            peer.peer_id,
            conn.remote_peer_shared_id,
            None,  # No invite for stub connections
            placeholder_key,
            conn.their_connection_id,
            placeholder_key,  # Same placeholder for their_key
            conn.created_at,
            conn.created_at,
            300_000  # 5 minute TTL
        ))
        peer.db.commit()

    def install(self) -> None:
        """Install stub send() as replacement for connection.send().

        This monkey-patches the connection module to route through our bridge.
        """
        if self._installed:
            return

        from events.network import connection as conn_module

        self._original_send = conn_module.send
        conn_module.send = self._stub_send
        self._installed = True

        if self.verbose:
            log.info("[STUB] Installed stub connection bridge")

    def uninstall(self) -> None:
        """Restore original connection.send()."""
        if not self._installed:
            return

        from events.network import connection as conn_module

        if self._original_send:
            conn_module.send = self._original_send
        self._installed = False

        if self.verbose:
            log.info("[STUB] Uninstalled stub connection bridge")

    def _stub_send(
        self,
        recorded_by: str,
        connection_id: str,
        blob: bytes,
        t_ms: int,
        db
    ) -> bool:
        """Stub replacement for connection.send().

        Routes messages through the bridge instead of encrypting and queuing.
        """
        # Find sender peer
        sender = self.peers.get(recorded_by)
        if not sender:
            log.warning(f"[STUB] Unknown sender peer: {recorded_by[:20]}...")
            return False

        # Find connection
        conn = sender.connections.get(connection_id)
        if not conn:
            # Try looking up in database
            safedb = create_safe_db(sender.db, recorded_by=recorded_by)
            row = safedb.query_one(
                "SELECT their_connection_id, peer_shared_id FROM connections WHERE connection_id = ?",
                (connection_id,)
            )
            if not row:
                log.warning(f"[STUB] Unknown connection: {connection_id[:20]}...")
                return False
            their_connection_id = row['their_connection_id']
            remote_peer_shared_id = row['peer_shared_id']
        else:
            their_connection_id = conn.their_connection_id
            remote_peer_shared_id = conn.remote_peer_shared_id

        # Find receiver by peer_shared_id
        receiver_peer_id = self.peer_shared_to_id.get(remote_peer_shared_id)
        if not receiver_peer_id:
            log.warning(f"[STUB] Unknown receiver: {remote_peer_shared_id[:20] if remote_peer_shared_id else 'None'}...")
            return False

        # Queue message for delivery
        msg = StubMessage(
            from_peer_id=recorded_by,
            to_peer_id=receiver_peer_id,
            connection_id=connection_id,
            reply_connection_id=their_connection_id,
            blob=blob,
            sent_at_ms=t_ms,
        )
        self.message_queue.append(msg)

        if self.verbose:
            # Try to parse message type for logging
            try:
                data = crypto.parse_json(blob)
                msg_type = data.get('type', 'unknown')
                inner_type = data.get('data', {}).get('type', '')
                if inner_type:
                    msg_type = f"{msg_type}/{inner_type}"
            except:
                msg_type = 'binary'

            sender_name = sender.name
            receiver = self.peers.get(receiver_peer_id)
            receiver_name = receiver.name if receiver else receiver_peer_id[:10]
            log.info(f"[STUB] {sender_name} -> {receiver_name}: {msg_type} ({len(blob)} bytes)")

        return True

    def deliver_all(self, t_ms: int) -> int:
        """Deliver all queued messages.

        Messages are written directly to receiver's incoming_blobs queue.
        Returns number of messages delivered.
        """
        delivered = 0

        while self.message_queue:
            msg = self.message_queue.popleft()

            receiver = self.peers.get(msg.to_peer_id)
            if not receiver:
                log.warning(f"[STUB] Receiver not found: {msg.to_peer_id}")
                continue

            # Write directly to receiver's incoming queue (bypass encryption)
            # We store the raw blob as if it had been unwrapped
            receiver.db.execute("""
                INSERT INTO incoming_blobs (blob, sent_at, deliver_at) VALUES (?, ?, ?)
            """, (msg.blob, t_ms, t_ms))
            receiver.db.commit()

            msg.delivered = True
            self.delivered_messages.append(msg)
            delivered += 1

        if self.verbose and delivered > 0:
            log.info(f"[STUB] Delivered {delivered} messages at t={t_ms}")

        return delivered

    def get_stats(self) -> dict:
        """Get bridge statistics."""
        return {
            'peers': len(self.peers),
            'queued': len(self.message_queue),
            'delivered': len(self.delivered_messages),
            'total_bytes': sum(len(m.blob) for m in self.delivered_messages),
        }

    def clear_history(self) -> None:
        """Clear delivered message history."""
        self.delivered_messages.clear()


# Context manager for easy test setup
class stub_bridge:
    """Context manager for stub connection bridge.

    Usage:
        with stub_bridge() as bridge:
            alice = bridge.add_peer("Alice")
            bob = bridge.add_peer("Bob")
            bridge.connect(alice, bob)
            # ... run sync tests ...
    """

    def __init__(self, verbose: bool = True):
        self.bridge = StubConnectionBridge(verbose=verbose)

    def __enter__(self) -> StubConnectionBridge:
        self.bridge.install()
        return self.bridge

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.bridge.uninstall()
        return False
