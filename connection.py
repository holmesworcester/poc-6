"""Connection abstraction for peer-to-peer communication.

Connections are bidirectional channels keyed by transit keys, labeled by identity.
They provide:
- send(connection_id, blob) - send to a specific connection
- receive(peer_id, ...) - SafeDB-scoped receive
- process_inbox() - device-wide routing to receive()

The connection layer handles:
- Early routing by transit key hint (before decryption)
- Peer scoping via recorded_by
- Identity labeling (peer_shared_id or invite_id for bootstrap)
"""
from dataclasses import dataclass
from typing import Any
import logging

import crypto
import store
from db import Database, create_safe_db, create_unsafe_db, SafeDB, UnsafeDB

log = logging.getLogger(__name__)


@dataclass
class Connection:
    """Bidirectional channel keyed by transit keys, labeled by identity."""

    our_transit_key_id: str          # Key we gave them (they send to us)
    recorded_by: str                 # Local peer who owns this connection

    # Identity labels (at least one set)
    peer_shared_id: str | None       # Remote peer's public identity (NULL until synced)
    invite_id: str | None            # Invite used for this connection (for bootstrap)

    # Their key (we send to them)
    their_transit_key_id: str | None
    their_transit_key: bytes | None

    # Network
    origin_ip: str | None
    origin_port: int | None

    # Lifecycle
    last_seen_ms: int
    ttl_ms: int

    @classmethod
    def from_row(cls, row: dict) -> 'Connection':
        """Create Connection from database row."""
        return cls(
            our_transit_key_id=row['our_transit_key_id'],
            recorded_by=row['recorded_by'],
            peer_shared_id=row.get('peer_shared_id'),
            invite_id=row.get('invite_id'),
            their_transit_key_id=row.get('their_transit_key_id'),
            their_transit_key=row.get('their_transit_key'),
            origin_ip=row.get('origin_ip'),
            origin_port=row.get('origin_port'),
            last_seen_ms=row['last_seen_ms'],
            ttl_ms=row['ttl_ms'],
        )

    @property
    def label(self) -> str:
        """Human-readable identity: peer_shared_id or invite_id."""
        return self.peer_shared_id or self.invite_id or self.our_transit_key_id

    def is_active(self, t_ms: int) -> bool:
        """Check if connection is still valid (not expired)."""
        return self.last_seen_ms + self.ttl_ms > t_ms

    def can_send(self) -> bool:
        """Check if we have their key to send to them."""
        return self.their_transit_key is not None


def send(connection_id: str, blob: bytes, t_ms: int, db: Database) -> bool:
    """Send blob via a specific connection.

    Args:
        connection_id: The our_transit_key_id of the connection
        blob: Raw blob to wrap and send
        t_ms: Current timestamp
        db: Database connection

    Returns:
        True if sent, False if connection not found or can't send
    """
    unsafedb = create_unsafe_db(db)

    # Look up connection (need their_transit_key to send)
    row = unsafedb.query_one("""
        SELECT their_transit_key_id, their_transit_key
        FROM connections
        WHERE our_transit_key_id = ?
          AND their_transit_key IS NOT NULL
          AND last_seen_ms + ttl_ms > ?
    """, (connection_id, t_ms))

    if not row:
        log.debug(f"connection.send: no active connection for {connection_id[:20]}...")
        return False

    # Wrap blob with their transit key
    to_key = {
        'id': crypto.b64decode(row['their_transit_key_id']),
        'key': row['their_transit_key'],
        'type': 'symmetric'
    }

    wrapped = crypto.wrap(blob, to_key, db)

    # Queue for delivery via incoming (will be routed back through network)
    import queues
    queues.incoming.add(wrapped, t_ms, unsafedb)

    log.debug(f"connection.send: sent {len(blob)} bytes via {connection_id[:20]}...")
    return True


def receive(peer_id: str, transit_key_id: str, blob: bytes, t_ms: int, db: Database) -> bool:
    """Process blob for a specific peer. SafeDB-scoped because peer_id is passed.

    Args:
        peer_id: Local peer who owns this connection
        transit_key_id: The transit key ID (hint from blob)
        blob: Raw wrapped blob
        t_ms: Current timestamp
        db: Database connection

    Returns:
        True if processed, False if connection not found
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    unsafedb = create_unsafe_db(db)

    # Look up connection (peer-scoped)
    conn_row = safedb.query_one("""
        SELECT * FROM connections
        WHERE our_transit_key_id = ? AND recorded_by = ?
    """, (transit_key_id, peer_id))

    if not conn_row:
        log.debug(f"connection.receive: no connection for key {transit_key_id[:20]}... peer {peer_id[:20]}...")
        return False

    # Get our transit key to unwrap
    key_row = unsafedb.query_one(
        "SELECT key FROM transit_keys WHERE key_id = ?",
        (transit_key_id,)
    )

    if not key_row:
        log.warning(f"connection.receive: transit key not found: {transit_key_id[:20]}...")
        return False

    # Unwrap the blob
    try:
        unwrapped = crypto.unwrap(blob, key_row['key'])
    except Exception as e:
        log.warning(f"connection.receive: unwrap failed: {e}")
        return False

    # Store the unwrapped event and create recorded entry
    event_id = store.add(unwrapped, unsafedb)

    # Create recorded event entry for this peer
    safedb.execute("""
        INSERT OR IGNORE INTO valid_events (event_id, recorded_by, recorded_at)
        VALUES (?, ?, ?)
    """, (event_id, peer_id, t_ms))

    # Trigger projection
    from events import registry
    event_data = crypto.parse_json(unwrapped)
    event_type = event_data.get('type')

    if event_type and event_type in registry.EVENT_TYPES:
        projector = registry.EVENT_TYPES[event_type].get('project')
        if projector:
            try:
                projector(event_id, peer_id, t_ms, db)
            except Exception as e:
                log.warning(f"connection.receive: projection failed for {event_type}: {e}")

    log.debug(f"connection.receive: processed event {event_id[:20]}... for peer {peer_id[:20]}...")
    return True


def process_inbox(t_ms: int, db: Database) -> int:
    """Drain inbox, route by transit_key_id to receive(peer_id).

    Args:
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Number of blobs processed
    """
    unsafedb = create_unsafe_db(db)

    entries = unsafedb.query("""
        SELECT id, our_transit_key_id, blob
        FROM connection_inbox
        ORDER BY received_at
    """)

    processed = 0
    for entry in entries:
        # Look up transit key to find owner peer
        key_row = unsafedb.query_one(
            "SELECT owner_peer_id FROM transit_keys WHERE key_id = ?",
            (entry['our_transit_key_id'],)
        )

        if key_row:
            # Route to peer-scoped receive
            if receive(key_row['owner_peer_id'], entry['our_transit_key_id'], entry['blob'], t_ms, db):
                processed += 1

        # Delete processed (or orphaned) entry
        unsafedb.execute("DELETE FROM connection_inbox WHERE id = ?", (entry['id'],))

    if processed > 0:
        log.info(f"connection.process_inbox: processed {processed}/{len(entries)} blobs")

    return processed


def route_incoming(blob: bytes, t_ms: int, db: Database) -> bool:
    """Route incoming blob to connection inbox by hint.

    Args:
        blob: Raw transit-wrapped blob (first 16 bytes are hint)
        t_ms: Current timestamp
        db: Database connection

    Returns:
        True if routed to inbox, False if unknown key (use fallback queue)
    """
    if len(blob) < 16:
        return False

    hint = blob[:16]
    hint_b64 = crypto.b64encode(hint)

    unsafedb = create_unsafe_db(db)

    # Check if we have a transit key matching this hint
    key_row = unsafedb.query_one(
        "SELECT key_id FROM transit_keys WHERE key_id = ?",
        (hint_b64,)
    )

    if key_row:
        # Route to connection inbox
        unsafedb.execute("""
            INSERT INTO connection_inbox (our_transit_key_id, blob, received_at)
            VALUES (?, ?, ?)
        """, (hint_b64, blob, t_ms))
        log.debug(f"connection.route_incoming: routed to inbox for key {hint_b64[:20]}...")
        return True

    return False


def get_connections(peer_id: str, t_ms: int, db: Database) -> list[Connection]:
    """Get all active connections for a peer. SafeDB-scoped.

    Args:
        peer_id: Local peer ID
        t_ms: Current timestamp
        db: Database connection

    Returns:
        List of active Connection objects
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    rows = safedb.query("""
        SELECT * FROM connections
        WHERE recorded_by = ? AND last_seen_ms + ttl_ms > ?
    """, (peer_id, t_ms))

    return [Connection.from_row(row) for row in rows]


def get_connection_by_peer(peer_id: str, remote_peer_shared_id: str, t_ms: int, db: Database) -> Connection | None:
    """Get connection by remote peer identity. SafeDB-scoped.

    Prefers connections that can send (have their_transit_key).
    When multiple connections exist to the same peer, returns the one with
    complete bidirectional keys if available.

    Args:
        peer_id: Local peer ID
        remote_peer_shared_id: Remote peer's public identity
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Connection if found and active, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Prefer connections that can send (have their_transit_key)
    # ORDER BY: connections with their_transit_key first, then by most recent
    row = safedb.query_one("""
        SELECT * FROM connections
        WHERE peer_shared_id = ? AND recorded_by = ? AND last_seen_ms + ttl_ms > ?
        ORDER BY (their_transit_key IS NOT NULL) DESC, last_seen_ms DESC
    """, (remote_peer_shared_id, peer_id, t_ms))

    return Connection.from_row(row) if row else None


def get_connection_by_invite(peer_id: str, invite_id: str, t_ms: int, db: Database) -> Connection | None:
    """Get connection by invite ID (for bootstrap). SafeDB-scoped.

    Args:
        peer_id: Local peer ID
        invite_id: Invite used for this connection
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Connection if found and active, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    row = safedb.query_one("""
        SELECT * FROM connections
        WHERE invite_id = ? AND recorded_by = ? AND last_seen_ms + ttl_ms > ?
    """, (invite_id, peer_id, t_ms))

    return Connection.from_row(row) if row else None


def upsert_connection(
    our_transit_key_id: str,
    recorded_by: str,
    peer_shared_id: str | None = None,
    invite_id: str | None = None,
    their_transit_key_id: str | None = None,
    their_transit_key: bytes | None = None,
    origin_ip: str | None = None,
    origin_port: int | None = None,
    t_ms: int = 0,
    ttl_ms: int = 300000,
    db: Database = None,
) -> None:
    """Insert or update a connection. SafeDB-scoped.

    Args:
        our_transit_key_id: Key we gave them
        recorded_by: Local peer who owns this connection
        peer_shared_id: Remote peer's public identity (optional)
        invite_id: Invite used for this connection (optional)
        their_transit_key_id: Key ID they provided (optional)
        their_transit_key: Key they provided (optional)
        origin_ip: Their IP address (optional)
        origin_port: Their port (optional)
        t_ms: Current timestamp
        ttl_ms: Time-to-live in ms
        db: Database connection
    """
    if not peer_shared_id and not invite_id:
        raise ValueError("At least one of peer_shared_id or invite_id must be provided")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    safedb.execute("""
        INSERT INTO connections (
            our_transit_key_id, recorded_by, peer_shared_id, invite_id,
            their_transit_key_id, their_transit_key, origin_ip, origin_port,
            last_seen_ms, ttl_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(our_transit_key_id, recorded_by) DO UPDATE SET
            peer_shared_id = COALESCE(excluded.peer_shared_id, peer_shared_id),
            invite_id = COALESCE(excluded.invite_id, invite_id),
            their_transit_key_id = COALESCE(excluded.their_transit_key_id, their_transit_key_id),
            their_transit_key = COALESCE(excluded.their_transit_key, their_transit_key),
            origin_ip = COALESCE(excluded.origin_ip, origin_ip),
            origin_port = COALESCE(excluded.origin_port, origin_port),
            last_seen_ms = excluded.last_seen_ms,
            ttl_ms = excluded.ttl_ms
    """, (
        our_transit_key_id, recorded_by, peer_shared_id, invite_id,
        their_transit_key_id, their_transit_key, origin_ip, origin_port,
        t_ms, ttl_ms
    ))

    log.debug(f"connection.upsert: {our_transit_key_id[:20]}... for peer {recorded_by[:20]}...")


def upgrade_identity(our_transit_key_id: str, recorded_by: str, peer_shared_id: str, db: Database) -> None:
    """Upgrade connection identity from invite_id to peer_shared_id.

    Called when peer_shared event projects, to associate the connection with the real identity.

    Args:
        our_transit_key_id: Key we gave them
        recorded_by: Local peer who owns this connection
        peer_shared_id: The newly-synced peer identity
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    safedb.execute("""
        UPDATE connections
        SET peer_shared_id = ?
        WHERE our_transit_key_id = ? AND recorded_by = ? AND peer_shared_id IS NULL
    """, (peer_shared_id, our_transit_key_id, recorded_by))

    log.debug(f"connection.upgrade_identity: {our_transit_key_id[:20]}... -> {peer_shared_id[:20]}...")


def purge_expired(t_ms: int, db: Database) -> int:
    """Remove expired connections for all local peers.

    Iterates through local peers to maintain peer-scoped access pattern.

    Args:
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Number of connections purged
    """
    unsafedb = create_unsafe_db(db)

    # Get all local peers
    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    total_count = 0
    for peer_row in local_peers:
        peer_id = peer_row['peer_id']
        safedb = create_safe_db(db, recorded_by=peer_id)

        # Count expired for this peer
        count_row = safedb.query_one(
            "SELECT COUNT(*) as cnt FROM connections WHERE recorded_by = ? AND last_seen_ms + ttl_ms < ?",
            (peer_id, t_ms)
        )
        count = count_row['cnt'] if count_row else 0

        if count > 0:
            safedb.execute(
                "DELETE FROM connections WHERE recorded_by = ? AND last_seen_ms + ttl_ms < ?",
                (peer_id, t_ms)
            )
            total_count += count

    if total_count > 0:
        log.info(f"connection.purge_expired: purged {total_count} expired connections")

    return total_count
