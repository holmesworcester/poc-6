"""Connection ack event type for acknowledging connection requests.

When a peer receives a connection_request, they create a connection_ack
with their own fresh symmetric key. The ack is wrapped with the requester's
symmetric key (from the request) and sent back.

When the requester receives the ack, their connection becomes bidirectional
(both parties have symmetric keys for each direction).

Auth model:
- Always signed by peer_shared (acks are only created after peer_shared exists)
- Implicit auth via decryption (ack was encrypted with our symmetric key)
"""

# Registry metadata
EVENT_TYPE = 'connection_ack'
SHAREABLE = False  # Local-only - contains symmetric key material
PROJECTION_TABLE = 'connections'

from typing import Any
import logging
from core import crypto
from core import store
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)

# Connection TTL (5 minutes default)
CONNECTION_TTL_MS = 300_000


# v2 event specification
# No signer verification needed - auth is via encryption (only someone who
# decrypted our request could have our symmetric key to encrypt this ack)
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,  # Implicit auth via decryption, no signature check
    'requires': {},
    'optional': {},
}


def create(
    for_request_id: str,
    from_peer_id: str,
    from_peer_shared_id: str,
    t_ms: int,
    db: Any
) -> tuple[str, bytes]:
    """Create a connection ack referencing a request.

    Args:
        for_request_id: The request's event ID being acknowledged
        from_peer_id: Local peer ID creating the ack
        from_peer_shared_id: Local peer's public identity
        t_ms: Timestamp
        db: Database connection

    Returns:
        (ack_id, symmetric_key): The ack's event ID and key bytes
    """
    from events.identity import peer

    log.debug(f"connection_ack.create: from={from_peer_shared_id[:20]}... for={for_request_id[:20]}...")

    # Generate fresh symmetric key for the ack
    symmetric_key = crypto.generate_secret()

    # Build connection ack event
    event_data = {
        'type': 'connection_ack',
        'for_request_id': for_request_id,
        'key': crypto.b64encode(symmetric_key),
        'signed_by': from_peer_shared_id,
        'signer_type': 'peer_shared',  # Acks are always peer_shared signed
        'created_at': t_ms,
        'ttl_ms': CONNECTION_TTL_MS,
    }

    # Sign with peer's private key
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store the event
    blob = crypto.canonicalize_json(signed_event)
    unsafedb = create_unsafe_db(db)
    ack_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)

    log.info(f"connection_ack.create: created {ack_id[:20]}... for request {for_request_id[:20]}...")

    return ack_id, symmetric_key


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for connection_ack events.

    Updates the existing connection entry with their_key and their_connection_id.

    Auth: No signature verification needed - implicit auth via encryption.
    Only someone who decrypted our request could have our symmetric key.
    """
    event_data = ctx.event_data
    event_id = ctx.event_id
    recorded_by = ctx.recorded_by
    recorded_at = ctx.recorded_at

    for_request_id = event_data.get('for_request_id')
    peer_shared_id = event_data.get('signed_by')
    key_b64 = event_data.get('key')

    if not for_request_id:
        log.warning(f"connection_ack.project_pure: missing for_request_id in {event_id[:20]}...")
        return ProjectorResult(writes=tuple(), valid_event=False)

    if not key_b64:
        log.warning(f"connection_ack.project_pure: missing key in {event_id[:20]}...")
        return ProjectorResult(writes=tuple(), valid_event=False)

    their_key = crypto.b64decode(key_b64)

    log.info(f"connection_ack.project_pure: updating connection {for_request_id[:20]}... with ack {event_id[:20]}...")

    # Update existing connection with their info
    writes = (
        WriteOp(
            op='update',
            table='connections',
            values={
                'their_connection_id': event_id,
                'their_key': their_key,
                'peer_shared_id': peer_shared_id,
                'last_handshake_ms': recorded_at,
            },
            where={
                'connection_id': for_request_id,
                'recorded_by': recorded_by,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def send_ack_for_request(
    request_id: str,
    remote_peer_shared_id: str | None,
    remote_invite_id: str | None,
    their_key: bytes,
    local_peer_id: str,
    t_ms: int,
    db: Any
) -> None:
    """Send connection ack in response to a request.

    Args:
        request_id: The connection request we're acknowledging
        remote_peer_shared_id: Remote peer's public identity (NULL in bootstrap mode)
        remote_invite_id: Invite used for this connection (set in bootstrap mode)
        their_key: Symmetric key from the request
        local_peer_id: Our local peer ID
        t_ms: Timestamp
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=local_peer_id)

    # Get our peer_shared_id
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (local_peer_id, local_peer_id)
    )
    if not peer_self_row:
        log.warning(f"connection_ack.send_ack_for_request: no peer_shared_id for {local_peer_id[:20]}...")
        return

    local_peer_shared_id = peer_self_row['peer_shared_id']

    # Always create a new connection for each request.
    # Old connections will expire via TTL. This avoids connection_id mismatch bugs
    # that occurred when trying to reuse existing connections.
    ack_id, ack_key = create(
        for_request_id=request_id,
        from_peer_id=local_peer_id,
        from_peer_shared_id=local_peer_shared_id,
        t_ms=t_ms,
        db=db
    )

    # Store our outgoing connection (we're the one who received the request)
    # Our connection_id is the ack_id, their_connection_id is the request_id
    # In bootstrap mode: peer_shared_id is NULL, invite_id is set
    # In normal mode: peer_shared_id is set, invite_id is NULL
    safedb.execute("""
        INSERT OR REPLACE INTO connections (
            connection_id, recorded_by, peer_shared_id, invite_id,
            our_key, their_connection_id, their_key,
            created_at, last_handshake_ms, ttl_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ack_id, local_peer_id, remote_peer_shared_id, remote_invite_id,
        ack_key, request_id, their_key,
        t_ms, t_ms, CONNECTION_TTL_MS
    ))

    # Wrap ack with their symmetric key and queue for delivery
    unsafedb = create_unsafe_db(db)
    ack_blob = store.get(ack_id, unsafedb)

    to_key = {
        'id': crypto.b64decode(request_id),  # Use request_id as hint
        'key': their_key,
        'type': 'symmetric'
    }

    wrapped = crypto.wrap(ack_blob, to_key, db)

    from core import queues
    # Pass peer IDs for NAT enforcement
    queues.incoming.add(wrapped, t_ms, unsafedb, from_peer=local_peer_id, to_peer=remote_peer_shared_id)

    # Remove from pending requests since we successfully acked
    safedb.execute(
        "DELETE FROM pending_connection_requests WHERE request_id = ? AND recorded_by = ?",
        (request_id, local_peer_id)
    )

    log.info(f"connection_ack.send_ack_for_request: sent ack {ack_id[:20]}... for request {request_id[:20]}...")


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Legacy projector for connection_ack events."""
    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"connection_ack.project: blob not found for {event_id[:20]}...")
        return None

    event_data = crypto.parse_json(blob)

    for_request_id = event_data.get('for_request_id')
    peer_shared_id = event_data.get('signed_by')
    key_b64 = event_data.get('key')

    if not for_request_id:
        log.warning(f"connection_ack.project: missing for_request_id in {event_id[:20]}...")
        return None

    if not key_b64:
        log.warning(f"connection_ack.project: missing key in {event_id[:20]}...")
        return None

    # Validation: Must reference a connection we created
    existing = safedb.query_one("""
        SELECT connection_id FROM connections
        WHERE connection_id = ? AND recorded_by = ?
    """, (for_request_id, recorded_by))

    if not existing:
        log.warning(f"connection_ack.project: no matching request for {event_id[:20]}...")
        return None

    # Auth: The encryption chain provides authentication for acks.
    # Only someone who decrypted our request (encrypted with their transit prekey)
    # could have our symmetric key to encrypt this ack.
    # Signature verification would create a catch-22: we'd need their peer_shared
    # to verify, but peer_shared syncs over the connection we're establishing.

    their_key = crypto.b64decode(key_b64)

    # Update connection with their info
    safedb.execute("""
        UPDATE connections
        SET their_connection_id = ?,
            their_key = ?,
            peer_shared_id = COALESCE(peer_shared_id, ?),
            last_handshake_ms = ?
        WHERE connection_id = ? AND recorded_by = ?
    """, (
        event_id, their_key, peer_shared_id,
        recorded_at, for_request_id, recorded_by
    ))

    log.info(f"connection_ack.project: updated connection {for_request_id[:20]}... with ack {event_id[:20]}...")

    return event_id
