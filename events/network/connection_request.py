"""Connection request event type for initiating peer connections.

When a peer wants to establish a connection, they create a connection_request
with a fresh symmetric key. The request is wrapped with the recipient's
transit prekey and sent.

The recipient projects this event into pending_connection_requests, then
creates a connection_ack in response.

Auth model (polymorphic based on signer_type):
- signer_type='peer_shared': normal mode, signed with peer_shared key
- signer_type='invite': bootstrap mode, signed with invite key
"""

# Registry metadata
EVENT_TYPE = 'connection_request'
SHAREABLE = False  # Local-only - contains symmetric key material
PROJECTION_TABLE = 'pending_connection_requests'

from typing import Any
import logging
from core import crypto
from core import store
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp, Command
from core.projection_v2 import register_command_handler

log = logging.getLogger(__name__)

# Connection TTL (5 minutes default)
CONNECTION_TTL_MS = 300_000


# v2 event specification
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {},
}


def create(
    to_peer_shared_id: str | None,
    from_peer_id: str,
    from_peer_shared_id: str | None,
    invite_id: str | None,
    t_ms: int,
    db: Any
) -> tuple[str, bytes]:
    """Create a connection request with fresh symmetric key.

    Args:
        to_peer_shared_id: Remote peer's public identity (NULL for bootstrap)
        from_peer_id: Local peer ID creating the request
        from_peer_shared_id: Local peer's public identity (NULL in bootstrap mode)
        invite_id: Invite ID for bootstrap connections
        t_ms: Timestamp
        db: Database connection

    Returns:
        (request_id, symmetric_key): The event ID and key bytes
    """
    from events.identity import peer

    if not to_peer_shared_id and not invite_id:
        raise ValueError("At least one of to_peer_shared_id or invite_id must be provided")

    # Determine signer_type and signed_by based on mode
    if from_peer_shared_id:
        signer_type = 'peer_shared'
        signed_by = from_peer_shared_id
    else:
        signer_type = 'invite'
        signed_by = invite_id

    log.debug(f"connection_request.create: from={signed_by[:20] if signed_by else 'unknown'}... to={to_peer_shared_id[:20] if to_peer_shared_id else invite_id[:20]}...")

    # Generate fresh symmetric key
    symmetric_key = crypto.generate_secret()

    # Build connection request event
    event_data = {
        'type': 'connection_request',
        'key': crypto.b64encode(symmetric_key),
        'to_peer_shared_id': to_peer_shared_id,
        'invite_id': invite_id,
        'signed_by': signed_by,
        'signer_type': signer_type,
        'created_at': t_ms,
        'ttl_ms': CONNECTION_TTL_MS,
    }

    # Sign based on signer_type
    if signer_type == 'invite':
        # Bootstrap mode: sign with invite key
        invite_private_key = _get_invite_private_key(from_peer_id, invite_id, db)
        if not invite_private_key:
            raise ValueError(f"No invite private key found for invite {invite_id[:20]}...")
        signed_event = crypto.sign_event(event_data, invite_private_key)
        log.debug(f"connection_request.create: signed with invite key for {invite_id[:20]}...")
    else:
        # Normal mode: sign with peer's private key
        private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
        signed_event = crypto.sign_event(event_data, private_key)

    # Store the event
    blob = crypto.canonicalize_json(signed_event)
    unsafedb = create_unsafe_db(db)
    request_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)

    log.info(f"connection_request.create: created {request_id[:20]}... for {to_peer_shared_id[:20] if to_peer_shared_id else invite_id[:20]}...")

    return request_id, symmetric_key


def _get_invite_private_key(peer_id: str, invite_id: str, db: Any) -> bytes | None:
    """Get the invite private key for signing bootstrap connection events.

    Looks in both group_prekeys (inviter's case) and invite_accepteds (joiner's case).
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # First try group_prekeys (inviter's case - we created the invite)
    invite_key_row = safedb.query_one(
        "SELECT private_key FROM group_prekeys WHERE owner_peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if invite_key_row and invite_key_row['private_key']:
        return invite_key_row['private_key']

    # Then try invite_accepteds (joiner's case - we accepted an invite)
    ia_row = safedb.query_one(
        "SELECT invite_private_key FROM invite_accepteds WHERE invite_id = ? AND recorded_by = ?",
        (invite_id, peer_id)
    )
    if ia_row and ia_row['invite_private_key']:
        return ia_row['invite_private_key']

    return None


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for connection_request events.

    Stores in pending_connection_requests and issues a command to send the ack.

    Auth is handled by the resolver via EVENT_SPEC signer config.
    """
    event_data = ctx.event_data
    event_id = ctx.event_id
    recorded_by = ctx.recorded_by
    recorded_at = ctx.recorded_at

    # Auth: resolver verified signature, check ctx.signer is present
    if not ctx.signer:
        log.warning(f"connection_request.project_pure: no signer for {event_id[:20]}...")
        return ProjectorResult(writes=tuple(), valid_event=False)

    signer_type = event_data.get('signer_type')
    signed_by = event_data.get('signed_by')
    key_b64 = event_data.get('key')

    # Determine identity labels based on signer_type:
    # - Bootstrap mode (signer_type='invite'): signed_by is invite_id, peer_shared_id is NULL
    # - Normal mode (signer_type='peer_shared'): signed_by is peer_shared_id
    if signer_type == 'invite':
        remote_peer_shared_id = None
        remote_invite_id = signed_by  # signed_by IS the invite_id in bootstrap mode
    else:
        remote_peer_shared_id = signed_by
        remote_invite_id = None

    # Extract key
    if not key_b64:
        log.warning(f"connection_request.project_pure: missing key in {event_id[:20]}...")
        return ProjectorResult(writes=tuple(), valid_event=False)

    symmetric_key = crypto.b64decode(key_b64)

    log.info(f"connection_request.project_pure: storing {event_id[:20]}... from {remote_peer_shared_id[:20] if remote_peer_shared_id else remote_invite_id[:20] if remote_invite_id else 'unknown'}...")

    # Store in pending_connection_requests
    writes = (
        WriteOp(
            op='insert',
            table='pending_connection_requests',
            values={
                'request_id': event_id,
                'remote_peer_shared_id': remote_peer_shared_id,
                'remote_invite_id': remote_invite_id,
                'their_key': symmetric_key,
                'received_at': recorded_at,
                'recorded_by': recorded_by,
            },
        ),
    )

    # Command to send the ack (side effect)
    commands = (
        Command(
            command_type='send_connection_ack',
            args={
                'request_id': event_id,
                'remote_peer_shared_id': remote_peer_shared_id,
                'remote_invite_id': remote_invite_id,
                'their_key': symmetric_key,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True, commands=commands)


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Legacy projector for connection_request events."""
    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"connection_request.project: blob not found for {event_id[:20]}...")
        return None

    event_data = crypto.parse_json(blob)

    signer_type = event_data.get('signer_type')
    signed_by = event_data.get('signed_by')
    invite_id = event_data.get('invite_id')
    key_b64 = event_data.get('key')

    # Determine identity labels based on signer_type:
    # - Bootstrap mode (signer_type='invite'): signed_by is invite_id, peer_shared_id is NULL
    # - Normal mode (signer_type='peer_shared'): signed_by is peer_shared_id
    if signer_type == 'invite':
        remote_peer_shared_id = None
        remote_invite_id = signed_by  # signed_by IS the invite_id in bootstrap mode
    else:
        remote_peer_shared_id = signed_by
        remote_invite_id = None

    # ENFORCEMENT: Reject connections from removed peers
    if remote_peer_shared_id:
        removed_check = unsafedb.query_one(
            "SELECT 1 FROM removed_peers WHERE peer_shared_id = ? LIMIT 1",
            (remote_peer_shared_id,)
        )
        if removed_check:
            log.info(f"connection_request.project: rejecting from removed peer {remote_peer_shared_id[:20]}...")
            return None

    # Authentication based on signer_type
    authenticated = False

    if signer_type == 'invite' and invite_id:
        # Bootstrap mode: verify main signature with invite pubkey
        # Use invite_id from event_data (not remote_invite_id) for pubkey lookup
        pubkey_row = safedb.query_one(
            """SELECT invite_pubkey FROM invites WHERE invite_id = ? AND recorded_by = ?
               UNION
               SELECT invite_pubkey FROM invite_accepteds WHERE invite_id = ? AND recorded_by = ?
               LIMIT 1""",
            (invite_id, recorded_by, invite_id, recorded_by)
        )
        if pubkey_row and pubkey_row['invite_pubkey']:
            invite_public_key = crypto.b64decode(pubkey_row['invite_pubkey'])
            if crypto.verify_event(event_data, invite_public_key):
                log.info(f"connection_request.project: invite signature verified for {invite_id[:20]}...")
                authenticated = True

    elif signer_type == 'peer_shared' and remote_peer_shared_id:
        # Normal mode: verify main signature with peer_shared pubkey
        if crypto.verify_signed_by_peer_shared(event_data, recorded_by, db):
            log.debug(f"connection_request.project: peer signature verified")
            authenticated = True

    if not authenticated:
        log.warning(f"connection_request.project: auth failed for {event_id[:20]}... signer_type={signer_type}")
        return None

    # Extract key
    if not key_b64:
        log.warning(f"connection_request.project: missing key in {event_id[:20]}...")
        return None

    symmetric_key = crypto.b64decode(key_b64)

    log.info(f"connection_request.project: storing {event_id[:20]}... from {remote_peer_shared_id[:20] if remote_peer_shared_id else remote_invite_id[:20] if remote_invite_id else 'unknown'}...")

    # Store in pending_connection_requests
    safedb.execute("""
        INSERT OR IGNORE INTO pending_connection_requests
        (request_id, remote_peer_shared_id, remote_invite_id, their_key, received_at, recorded_by)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (event_id, remote_peer_shared_id, remote_invite_id, symmetric_key, recorded_at, recorded_by))

    # Try to send ack immediately (may fail if no peer_self yet, that's OK - will retry in send_to_all)
    from events.network import connection_ack
    connection_ack.send_ack_for_request(
        event_id, remote_peer_shared_id, remote_invite_id, symmetric_key, recorded_by, recorded_at, db
    )

    return event_id


# ============================================================================
# Connection management functions (moved from utility connection.py)
# ============================================================================

from dataclasses import dataclass


@dataclass
class Connection:
    """Bidirectional channel identified by connection_id, labeled by identity."""

    connection_id: str               # Our connection event ID (mode=req)
    recorded_by: str                 # Local peer who owns this connection

    # Identity labels (at least one set)
    peer_shared_id: str | None       # Remote peer's public identity (NULL until synced)
    invite_id: str | None            # Invite used for this connection (for bootstrap)

    # Keys
    our_key: bytes                   # Symmetric key we created (they send to us)
    their_connection_id: str | None  # Their ack's connection_id (NULL until they ack)
    their_key: bytes | None          # Symmetric key they created (we send to them)

    # Lifecycle
    created_at: int
    last_handshake_ms: int
    ttl_ms: int

    # Sync optimization
    last_synced_root_hash: bytes | None = None  # Skip sync if our root unchanged

    # Network address (LEARNED from packets)
    peer_ip: str | None = None              # Learned from UDP source
    peer_port: int | None = None            # Learned from UDP source
    address_source: str | None = None       # 'packet', 'invite', 'manual'
    address_learned_ms: int | None = None   # When we learned it

    @classmethod
    def from_row(cls, row: dict) -> 'Connection':
        """Create Connection from database row."""
        return cls(
            connection_id=row['connection_id'],
            recorded_by=row['recorded_by'],
            peer_shared_id=row.get('peer_shared_id'),
            invite_id=row.get('invite_id'),
            our_key=row['our_key'],
            their_connection_id=row.get('their_connection_id'),
            their_key=row.get('their_key'),
            created_at=row['created_at'],
            last_handshake_ms=row['last_handshake_ms'],
            ttl_ms=row['ttl_ms'],
            last_synced_root_hash=row.get('last_synced_root_hash'),
            peer_ip=row.get('peer_ip'),
            peer_port=row.get('peer_port'),
            address_source=row.get('address_source'),
            address_learned_ms=row.get('address_learned_ms'),
        )

    @property
    def label(self) -> str:
        """Human-readable identity: peer_shared_id or invite_id."""
        return self.peer_shared_id or self.invite_id or self.connection_id

    @property
    def short_label(self) -> str:
        """Short version of label for display (first 8 chars)."""
        label = self.label
        return label[:8] + '...' if len(label) > 11 else label

    @property
    def short_connection_id(self) -> str:
        """Short connection_id for display (first 11 chars)."""
        return self.connection_id[:11] + '...' if len(self.connection_id) > 14 else self.connection_id

    def is_active(self, t_ms: int) -> bool:
        """Check if connection is still valid (not expired)."""
        return self.last_handshake_ms + self.ttl_ms > t_ms

    def can_send(self) -> bool:
        """Check if we have their key to send to them."""
        return self.their_key is not None

    def is_pending(self) -> bool:
        """Check if connection is pending (awaiting ack)."""
        return self.their_connection_id is None

    def is_bidirectional(self) -> bool:
        """Check if connection is fully established (both directions)."""
        return self.their_connection_id is not None and self.their_key is not None

    def is_bootstrap(self) -> bool:
        """Check if this is a bootstrap connection (via invite)."""
        return self.invite_id is not None

    def has_address(self) -> bool:
        """Check if we have a learned address for this connection."""
        return self.peer_ip is not None and self.peer_port is not None

    def get_address(self) -> tuple | None:
        """Get the learned address as (ip, port) tuple."""
        if self.has_address():
            return (self.peer_ip, self.peer_port)
        return None

    def time_since_handshake(self, t_ms: int) -> int:
        """Milliseconds since last handshake (req/ack)."""
        return t_ms - self.last_handshake_ms

    def time_since_traffic(self, t_ms: int) -> int | None:
        """Milliseconds since last traffic. STUB: returns None until implemented."""
        # TODO: Implement when last_traffic_ms is populated
        return None

    def time_until_expiry(self, t_ms: int) -> int:
        """Milliseconds until expiry."""
        return (self.last_handshake_ms + self.ttl_ms) - t_ms


def send_to_all(t_ms: int, db: Any) -> None:
    """Send connection requests from all local peers to all known peers.

    Called by tick() to establish/refresh connections.
    Skips peers we already have active connections with.

    Args:
        t_ms: Current timestamp in milliseconds
        db: Database connection
    """
    from events.network import connection_ack, connection_prekey

    unsafedb = create_unsafe_db(db)
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")

    log.info(f"connection_request.send_to_all: found {len(local_peer_rows)} local peers at t_ms={t_ms}")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']
        safedb = create_safe_db(db, recorded_by=peer_id)

        # Get our peer_shared_id (may be None during bootstrap before cascade completes)
        peer_self_row = safedb.query_one(
            "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
            (peer_id, peer_id)
        )
        peer_shared_id = peer_self_row['peer_shared_id'] if peer_self_row else None

        # Get invite data if joiner - includes inviter's peer_shared_id and transit prekey
        # Check this FIRST because bootstrap mode needs it even without peer_self
        invite_row = safedb.query_one(
            """SELECT invite_id, inviter_peer_shared_id,
                      inviter_transit_prekey_id, inviter_transit_prekey_public_key
               FROM invite_accepteds WHERE recorded_by = ? LIMIT 1""",
            (peer_id,)
        )
        invite_id = invite_row['invite_id'] if invite_row else None

        # BOOTSTRAP MODE: No peer_self yet, but we have invite_accepteds
        # Connect to inviter using invite authentication
        if not peer_shared_id:
            if invite_row and invite_row['inviter_peer_shared_id']:
                inviter_id = invite_row['inviter_peer_shared_id']
                log.info(f"connection_request.send_to_all: BOOTSTRAP MODE for {peer_id[:20]}... -> inviter {inviter_id[:20]}...")

                # Check if we already have a connection to inviter
                existing = safedb.query_one("""
                    SELECT 1 FROM connections
                    WHERE peer_shared_id = ? AND recorded_by = ?
                      AND last_handshake_ms + ttl_ms > ?
                """, (inviter_id, peer_id, t_ms))

                if not existing:
                    # Build transit prekey dict from invite_accepteds
                    inviter_transit_prekey = None
                    if invite_row.get('inviter_transit_prekey_public_key'):
                        inviter_transit_prekey = {
                            'id': crypto.b64decode(invite_row['inviter_transit_prekey_id']),
                            'public_key': invite_row['inviter_transit_prekey_public_key'],
                            'type': 'asymmetric'
                        }

                    try:
                        _send_request(
                            to_peer_shared_id=inviter_id,
                            from_peer_id=peer_id,
                            from_peer_shared_id=None,  # Bootstrap mode - no peer_shared_id yet
                            invite_id=invite_id,
                            t_ms=t_ms,
                            db=db,
                            inviter_transit_prekey=inviter_transit_prekey,
                        )
                    except Exception as e:
                        log.warning(f"connection_request.send_to_all: bootstrap error: {e}")
            continue  # Skip normal mode for this peer

        # NORMAL MODE: Have peer_self, can connect to all known peers

        # First, process any pending connection requests that we couldn't ack earlier
        pending_requests = safedb.query_all("""
            SELECT request_id, remote_peer_shared_id, remote_invite_id, their_key, received_at
            FROM pending_connection_requests WHERE recorded_by = ?
        """, (peer_id,))

        for pending in pending_requests:
            log.info(f"connection_request.send_to_all: processing pending request {pending['request_id'][:20]}...")
            connection_ack.send_ack_for_request(
                pending['request_id'],
                pending['remote_peer_shared_id'],
                pending['remote_invite_id'],  # May be set in bootstrap mode
                pending['their_key'],
                peer_id,
                t_ms,
                db
            )

        # Two types of connections:
        # 1. peers_shared: actual synced peers (peer_shared auth, permanent)
        # 2. transit_prekeys_shared: includes predicted IDs from invites (invite auth, expires)
        all_peer_ids = set()

        for row in safedb.query("SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?", (peer_id,)):
            all_peer_ids.add(row['peer_shared_id'])

        # Also include peers we have transit_prekeys_shared for (may include predicted invite IDs)
        for row in safedb.query(
            "SELECT peer_id FROM transit_prekeys_shared WHERE recorded_by = ?",
            (peer_id,)
        ):
            all_peer_ids.add(row['peer_id'])

        # Add inviter's peer_shared_id from invite_accepteds
        if invite_row and invite_row['inviter_peer_shared_id']:
            all_peer_ids.add(invite_row['inviter_peer_shared_id'])
            log.debug(f"connection_request.send_to_all: added inviter {invite_row['inviter_peer_shared_id'][:20]}... from invite_accepteds")

        # Send to each peer
        for to_peer_shared_id in all_peer_ids:
            if to_peer_shared_id == peer_shared_id:
                continue  # Skip self

            # Skip if already have ANY unexpired connection (active or pending)
            # We only need one pending request per peer - no point sending duplicates
            existing = safedb.query_one("""
                SELECT 1 FROM connections
                WHERE peer_shared_id = ? AND recorded_by = ?
                  AND last_handshake_ms + ttl_ms > ?
            """, (to_peer_shared_id, peer_id, t_ms))

            if existing:
                log.debug(f"connection_request.send_to_all: skipping {to_peer_shared_id[:20]}... (connection exists)")
                continue

            try:
                _send_request(
                    to_peer_shared_id=to_peer_shared_id,
                    from_peer_id=peer_id,
                    from_peer_shared_id=peer_shared_id,
                    invite_id=invite_id,
                    t_ms=t_ms,
                    db=db
                )
            except Exception as e:
                log.warning(f"connection_request.send_to_all: error sending to {to_peer_shared_id[:20]}...: {e}")


def _send_request(
    to_peer_shared_id: str,
    from_peer_id: str,
    from_peer_shared_id: str | None,
    invite_id: str | None,
    t_ms: int,
    db: Any,
    inviter_transit_prekey: dict | None = None,
) -> None:
    """Send a connection request to a specific peer.

    Args:
        to_peer_shared_id: Remote peer's public identity
        from_peer_id: Local peer ID
        from_peer_shared_id: Local peer's public identity (None in bootstrap mode)
        invite_id: Invite ID for bootstrap auth
        t_ms: Timestamp
        db: Database connection
        inviter_transit_prekey: Pre-fetched transit prekey dict (for bootstrap mode)
    """
    from events.network import connection_prekey

    # Create request
    connection_id, symmetric_key = create(
        to_peer_shared_id=to_peer_shared_id,
        from_peer_id=from_peer_id,
        from_peer_shared_id=from_peer_shared_id,
        invite_id=invite_id,
        t_ms=t_ms,
        db=db
    )

    # Store our outgoing connection (PENDING)
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    safedb.execute("""
        INSERT OR REPLACE INTO connections (
            connection_id, recorded_by, peer_shared_id, invite_id,
            our_key, their_connection_id, their_key,
            created_at, last_handshake_ms, ttl_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        connection_id, from_peer_id, to_peer_shared_id, invite_id,
        symmetric_key, None, None,  # PENDING - no their_* yet
        t_ms, t_ms, CONNECTION_TTL_MS
    ))

    # Get recipient's connection_prekey - try multiple sources
    to_key = None

    # 1. Pre-passed transit prekey (bootstrap mode with prekey from invite_accepteds)
    if inviter_transit_prekey:
        to_key = inviter_transit_prekey
        log.debug(f"connection_request._send_request: using pre-passed transit prekey")

    # 2. Look up from transit_prekeys_shared table (normal mode)
    if not to_key:
        to_key = connection_prekey.get_prekey_for_peer(to_peer_shared_id, from_peer_id, db)

    # 3. BOOTSTRAP FALLBACK: Check invite_accepteds for inviter's transit prekey
    if not to_key:
        inviter_row = safedb.query_one("""
            SELECT inviter_transit_prekey_id, inviter_transit_prekey_public_key
            FROM invite_accepteds
            WHERE inviter_peer_shared_id = ? AND recorded_by = ?
            LIMIT 1
        """, (to_peer_shared_id, from_peer_id))

        if inviter_row and inviter_row['inviter_transit_prekey_public_key']:
            to_key = {
                'id': crypto.b64decode(inviter_row['inviter_transit_prekey_id']),
                'public_key': inviter_row['inviter_transit_prekey_public_key'],
                'type': 'asymmetric'
            }
            log.info(f"connection_request._send_request: using inviter transit prekey from invite_accepteds for {to_peer_shared_id[:20]}...")

    if not to_key:
        log.warning(f"connection_request._send_request: no prekey for {to_peer_shared_id[:20]}...")
        return

    # Wrap and queue
    unsafedb = create_unsafe_db(db)
    request_blob = store.get(connection_id, unsafedb)
    wrapped = crypto.wrap(request_blob, to_key, db)

    from core import queues
    # Pass peer IDs for NAT enforcement
    queues.incoming.add(wrapped, t_ms, unsafedb, from_peer=from_peer_id, to_peer=to_peer_shared_id)

    log.info(f"connection_request._send_request: sent {connection_id[:20]}... to {to_peer_shared_id[:20]}...")


def purge_expired(t_ms: int, db: Any) -> int:
    """Remove expired connections.

    Args:
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Number of connections purged
    """
    unsafedb = create_unsafe_db(db)
    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    total_count = 0
    for peer_row in local_peers:
        peer_id = peer_row['peer_id']
        safedb = create_safe_db(db, recorded_by=peer_id)

        count_row = safedb.query_one(
            "SELECT COUNT(*) as cnt FROM connections WHERE recorded_by = ? AND last_handshake_ms + ttl_ms < ?",
            (peer_id, t_ms)
        )
        count = count_row['cnt'] if count_row else 0

        if count > 0:
            safedb.execute(
                "DELETE FROM connections WHERE recorded_by = ? AND last_handshake_ms + ttl_ms < ?",
                (peer_id, t_ms)
            )
            total_count += count

    if total_count > 0:
        log.info(f"connection_request.purge_expired: purged {total_count} expired connections")

    return total_count


# ============================================================================
# Connection display helpers
# ============================================================================

def list_all_for_display(peer_id: str, t_ms: int, db: Any) -> dict:
    """Get all connection data organized for CLI display.

    Args:
        peer_id: Local peer ID
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Dict with:
            'active': List of bidirectional connections
            'pending': List of pending (awaiting ack) connections
            'bootstrap': List of bootstrap connections (via invite)
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    rows = safedb.query("""
        SELECT * FROM connections
        WHERE recorded_by = ? AND last_handshake_ms + ttl_ms > ?
        ORDER BY last_handshake_ms DESC
    """, (peer_id, t_ms))

    connections = [Connection.from_row(row) for row in rows]

    result = {
        'active': [],
        'pending': [],
        'bootstrap': [],
    }

    for conn in connections:
        if conn.is_bidirectional():
            result['active'].append(conn)
        elif conn.is_pending():
            result['pending'].append(conn)
        elif conn.is_bootstrap():
            result['bootstrap'].append(conn)

    return result


def get_active_connection(peer_id: str, remote_peer_shared_id: str, t_ms: int, db: Any) -> Connection | None:
    """Get active bidirectional connection to a specific peer."""
    safedb = create_safe_db(db, recorded_by=peer_id)

    row = safedb.query_one("""
        SELECT * FROM connections
        WHERE peer_shared_id = ?
          AND recorded_by = ?
          AND their_connection_id IS NOT NULL
          AND their_key IS NOT NULL
          AND last_handshake_ms + ttl_ms > ?
        ORDER BY last_handshake_ms DESC
    """, (remote_peer_shared_id, peer_id, t_ms))

    return Connection.from_row(row) if row else None


def has_active_connection(peer_id: str, remote_peer_shared_id: str, t_ms: int, db: Any) -> bool:
    """Check if there's an active bidirectional connection to a peer."""
    return get_active_connection(peer_id, remote_peer_shared_id, t_ms, db) is not None


def format_time_ago(ms: int) -> str:
    """Format milliseconds as human-readable time ago string."""
    if ms < 1000:
        return f"{ms}ms ago"
    elif ms < 60000:
        return f"{ms // 1000}s ago"
    elif ms < 3600000:
        return f"{ms // 60000}m ago"
    else:
        return f"{ms // 3600000}h ago"


def format_time_remaining(ms: int) -> str:
    """Format milliseconds as human-readable time remaining string."""
    if ms <= 0:
        return "expired"
    elif ms < 60000:
        return f"in {ms // 1000}s"
    elif ms < 3600000:
        return f"in {ms // 60000}m {(ms % 60000) // 1000}s"
    else:
        return f"in {ms // 3600000}h {(ms % 3600000) // 60000}m"


# ============================================================================
# Address learning and lookup
# ============================================================================

def learn_address(connection_id: str, peer_ip: str, peer_port: int,
                  source: str, t_ms: int, recorded_by: str, db: Any) -> None:
    """Update connection with learned address from incoming packet.

    Called by the connection layer when processing incoming blobs
    to record the sender's network address.

    Args:
        connection_id: The connection that received the packet
        peer_ip: IP address learned from UDP source
        peer_port: Port learned from UDP source
        source: How we learned it ('packet', 'invite', 'manual')
        t_ms: When we learned it
        recorded_by: Local peer who owns this connection
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute("""
        UPDATE connections SET
            peer_ip = ?, peer_port = ?,
            address_source = ?, address_learned_ms = ?
        WHERE connection_id = ? AND recorded_by = ?
    """, (peer_ip, peer_port, source, t_ms, connection_id, recorded_by))
    log.debug(f"connection_request.learn_address: learned {peer_ip}:{peer_port} for connection {connection_id[:20]}...")


def get_address(connection_id: str, recorded_by: str, db: Any) -> tuple | None:
    """Get address for a connection, checking all sources.

    Priority:
    1. Learned address on connection (from recent packets)
    2. Bootstrap address from invite_accepteds

    Args:
        connection_id: The connection to get address for
        recorded_by: Local peer who owns this connection
        db: Database connection

    Returns:
        (ip, port) tuple if address found, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Priority 1: Learned address on connection
    conn = safedb.query_one("""
        SELECT peer_ip, peer_port, peer_shared_id, invite_id
        FROM connections WHERE connection_id = ? AND recorded_by = ?
    """, (connection_id, recorded_by))

    if not conn:
        return None

    if conn['peer_ip']:
        return (conn['peer_ip'], conn['peer_port'])

    # Priority 2: Bootstrap address from invite_accepteds
    if conn['peer_shared_id']:
        invite = safedb.query_one("""
            SELECT address, port FROM invite_accepteds
            WHERE inviter_peer_shared_id = ? AND recorded_by = ?
        """, (conn['peer_shared_id'], recorded_by))
        if invite and invite['address']:
            return (invite['address'], invite['port'])

    return None


def get_address_by_peer(peer_shared_id: str, recorded_by: str, db: Any) -> tuple | None:
    """Get address for a remote peer, checking multiple sources.

    Used when we need to send to a peer but don't have a specific connection_id.
    Checks in priority order:
    1. connections table (learned addresses)
    2. pending_connection_requests (source address from incoming request)
    3. invite_accepteds (bootstrap address from invite link)

    Args:
        peer_shared_id: Remote peer's public identity
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        (ip, port) tuple if address found, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Priority 1: Check connections with learned addresses
    conn = safedb.query_one("""
        SELECT peer_ip, peer_port FROM connections
        WHERE peer_shared_id = ? AND recorded_by = ? AND peer_ip IS NOT NULL
        ORDER BY address_learned_ms DESC LIMIT 1
    """, (peer_shared_id, recorded_by))

    if conn and conn['peer_ip']:
        return (conn['peer_ip'], conn['peer_port'])

    # Priority 2: Check pending_connection_requests (source address from incoming packets)
    pending = safedb.query_one("""
        SELECT source_ip, source_port FROM pending_connection_requests
        WHERE remote_peer_shared_id = ? AND recorded_by = ? AND source_ip IS NOT NULL
    """, (peer_shared_id, recorded_by))

    if pending and pending['source_ip']:
        return (pending['source_ip'], pending['source_port'])

    # Priority 3: Check invite_accepteds for bootstrap address
    invite = safedb.query_one("""
        SELECT address, port FROM invite_accepteds
        WHERE inviter_peer_shared_id = ? AND recorded_by = ?
    """, (peer_shared_id, recorded_by))

    if invite and invite['address']:
        return (invite['address'], invite['port'])

    return None


# ============================================================================
# User removal support
# ============================================================================

def remove_connections_for_user(user_id: str, db: Any) -> int:
    """Remove all connections associated with a user's peers.

    Called when a user is removed from the network. This removes all connections
    where the remote peer belongs to the specified user, across all local peers.

    Args:
        user_id: The user being removed
        db: Database connection

    Returns:
        Total number of connections removed
    """
    unsafedb = create_unsafe_db(db)
    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    total_removed = 0

    for peer_row in local_peers:
        peer_id = peer_row['peer_id']
        safedb = create_safe_db(db, recorded_by=peer_id)

        # Find peer_shared_ids that belong to this user
        user_peer_rows = safedb.query("""
            SELECT peer_shared_id FROM peers_shared
            WHERE user_id = ? AND recorded_by = ?
        """, (user_id, peer_id))

        for user_peer_row in user_peer_rows:
            peer_shared_id = user_peer_row['peer_shared_id']

            # Count before delete
            count_row = safedb.query_one(
                "SELECT COUNT(*) as cnt FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
                (peer_shared_id, peer_id)
            )
            count = count_row['cnt'] if count_row else 0

            if count > 0:
                safedb.execute(
                    "DELETE FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
                    (peer_shared_id, peer_id)
                )
                total_removed += count
                log.info(f"connection_request.remove_connections_for_user: removed {count} connections to peer {peer_shared_id[:20]}... for user {user_id[:20]}...")

    if total_removed > 0:
        log.info(f"connection_request.remove_connections_for_user: total {total_removed} connections removed for user {user_id[:20]}...")

    return total_removed


def remove_connections_for_peer(peer_shared_id: str, db: Any) -> int:
    """Remove all connections to a specific peer.

    Called when a peer is removed. This removes all connections to the specified
    peer_shared_id across all local peers.

    Args:
        peer_shared_id: The peer being removed
        db: Database connection

    Returns:
        Total number of connections removed
    """
    unsafedb = create_unsafe_db(db)
    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    total_removed = 0

    for peer_row in local_peers:
        peer_id = peer_row['peer_id']
        safedb = create_safe_db(db, recorded_by=peer_id)

        # Count before delete
        count_row = safedb.query_one(
            "SELECT COUNT(*) as cnt FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
            (peer_shared_id, peer_id)
        )
        count = count_row['cnt'] if count_row else 0

        if count > 0:
            safedb.execute(
                "DELETE FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
                (peer_shared_id, peer_id)
            )
            total_removed += count

    if total_removed > 0:
        log.info(f"connection_request.remove_connections_for_peer: removed {total_removed} connections to peer {peer_shared_id[:20]}...")

    return total_removed


# ============================================================================
# Connection query functions (used by sync.py and others)
# ============================================================================

def get_connections(peer_id: str, t_ms: int, db: Any) -> list[Connection]:
    """Get all active connections for a peer.

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
        WHERE recorded_by = ? AND last_handshake_ms + ttl_ms > ?
    """, (peer_id, t_ms))

    return [Connection.from_row(row) for row in rows]


def get_connection_by_peer(peer_id: str, remote_peer_shared_id: str, t_ms: int, db: Any) -> Connection | None:
    """Get connection by remote peer identity.

    Prefers connections that can send (have their_key).

    Args:
        peer_id: Local peer ID
        remote_peer_shared_id: Remote peer's public identity
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Connection if found and active, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Prefer connections with their_key (can send)
    row = safedb.query_one("""
        SELECT * FROM connections
        WHERE peer_shared_id = ? AND recorded_by = ? AND last_handshake_ms + ttl_ms > ?
        ORDER BY (their_key IS NOT NULL) DESC, last_handshake_ms DESC
    """, (remote_peer_shared_id, peer_id, t_ms))

    return Connection.from_row(row) if row else None


def get_connection_by_invite(peer_id: str, invite_id: str, t_ms: int, db: Any) -> Connection | None:
    """Get connection by invite ID (for bootstrap).

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
        WHERE invite_id = ? AND recorded_by = ? AND last_handshake_ms + ttl_ms > ?
    """, (invite_id, peer_id, t_ms))

    return Connection.from_row(row) if row else None


def get_connection_by_their_id(peer_id: str, their_connection_id: str, t_ms: int, db: Any) -> Connection | None:
    """Get connection by the remote party's connection_id.

    When receiving a message from a connection, the sender's connection_id
    is in the message envelope. We need to find OUR connection where
    their_connection_id matches the sender's connection_id.

    Args:
        peer_id: Local peer ID
        their_connection_id: Remote party's connection_id (from received message)
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Connection if found and active, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    row = safedb.query_one("""
        SELECT * FROM connections
        WHERE their_connection_id = ? AND recorded_by = ? AND last_handshake_ms + ttl_ms > ?
    """, (their_connection_id, peer_id, t_ms))

    return Connection.from_row(row) if row else None


# ============================================================================
# Sending on connections
# ============================================================================

def send(recorded_by: str, connection_id: str, blob: bytes, t_ms: int, db: Any) -> bool:
    """Send a blob on an established connection.

    THE interface for all outbound traffic on connections. Handles:
    - Looking up connection by connection_id
    - Wrapping blob with their_key (symmetric)
    - Adding hint (their_connection_id, full 32 bytes)
    - Queuing to incoming

    Args:
        recorded_by: Local peer ID
        connection_id: Our connection_id for this connection
        blob: Raw blob to send (event data)
        t_ms: Current timestamp
        db: Database connection

    Returns:
        True if sent, False if connection not ready (no their_key)
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    conn = safedb.query_one("""
        SELECT their_connection_id, their_key, peer_shared_id
        FROM connections
        WHERE connection_id = ? AND recorded_by = ?
    """, (connection_id, recorded_by))

    if not conn:
        log.warning(f"connection_request.send: no connection {connection_id[:20]}...")
        return False

    their_connection_id = conn['their_connection_id']
    their_key = conn['their_key']
    to_peer_shared_id = conn['peer_shared_id']

    if not their_key or not their_connection_id:
        log.debug(f"connection_request.send: connection {connection_id[:20]}... not ready (no their_key)")
        return False

    # Wrap with their key using their_connection_id as hint (full 32 bytes)
    to_key = {
        'id': crypto.b64decode(their_connection_id),
        'key': their_key,
        'type': 'symmetric'
    }

    wrapped = crypto.wrap(blob, to_key, db)

    unsafedb = create_unsafe_db(db)
    from core import queues
    # Pass peer IDs for NAT enforcement
    queues.incoming.add(wrapped, t_ms, unsafedb, from_peer=recorded_by, to_peer=to_peer_shared_id)

    log.debug(f"connection_request.send: sent {len(blob)}B on {connection_id[:20]}...")
    return True


# ============================================================================
# Command handler registration
# ============================================================================

def _handle_send_connection_ack(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Handle send_connection_ack command.

    Sends a connection ack in response to a connection request.
    """
    from events.network import connection_ack

    connection_ack.send_ack_for_request(
        request_id=args['request_id'],
        remote_peer_shared_id=args.get('remote_peer_shared_id'),
        remote_invite_id=args.get('remote_invite_id'),
        their_key=args['their_key'],
        local_peer_id=recorded_by,
        t_ms=recorded_at,
        db=db,
    )


# Register the command handler at module load time
register_command_handler('send_connection_ack', _handle_send_connection_ack)
