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

# Wire format constants
WIRE_TYPE_CODE = 0x33  # TYPE_CONNECTION_ACK
WIRE_PLAINTEXT_SIZE = 344  # CONNECTION_ACK_PLAINTEXT_SIZE
SECRET_SIZE = 32

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for connection_ack event type

def encode_plaintext(for_request_id: bytes, key: bytes) -> bytes:
    """Encode a connection_ack payload plaintext.

    Layout (344 bytes):
    - for_request_id (16)
    - key (32)
    - pad (296)
    """
    wire_format._require_len("for_request_id", for_request_id, 16)
    wire_format._require_len("key", key, SECRET_SIZE)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = for_request_id
    payload[16:48] = key
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a connection_ack payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"connection_ack plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {
        "for_request_id": data[0:16],
        "key": data[16:48],
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a connection_ack wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    for_request_id_b64: str,
    key: bytes,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    ttl_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a complete connection_ack wire event."""
    for_request_id = crypto.b64decode(for_request_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_plaintext(for_request_id=for_request_id, key=key)
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=0,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=ttl_ms,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = wire_format._pad_payload(plaintext)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a connection_ack wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for connection_ack")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    return {
        "type": EVENT_TYPE,
        "for_request_id": crypto.b64encode(decoded["for_request_id"]),
        "key": crypto.b64encode(decoded["key"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "ttl_ms": header.ttl_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }


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

    # Sign with peer's private key
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)

    blob = encode_wire_event(
        for_request_id_b64=for_request_id,
        key=symmetric_key,
        signed_by_b64=from_peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        ttl_ms=CONNECTION_TTL_MS,
        private_key=private_key,
    )
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

    _wire_shadow_connection_ack(for_request_id, key_b64)

    their_key = crypto.b64decode(key_b64)

    log.info(f"connection_ack.project_pure: updating connection {for_request_id[:20]}... with ack {event_id[:20]}...")

    # Update existing connection with their info
    writes = (
        WriteOp(
            op='update',
            table='connections',
            values={
                'their_key_id': event_id,
                'their_key': their_key,
                'peer_shared_id': peer_shared_id,
                'last_handshake_ms': recorded_at,
            },
            where={
                'key_id': for_request_id,
                'recorded_by': recorded_by,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_connection_ack(for_request_id: str, key_b64: str) -> None:
    """Validate connection_ack fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        for_request_id=crypto.b64decode(for_request_id),
        key=crypto.b64decode(key_b64),
    )
    decoded = decode_plaintext(plaintext)
    if decoded["for_request_id"] != crypto.b64decode(for_request_id):
        raise ValueError("wire shadow decode for_request_id mismatch")


def send_ack_for_request(
    request_id: str,
    remote_peer_shared_id: str | None,
    remote_invite_id: str | None,
    their_key: bytes,
    local_peer_id: str,
    t_ms: int,
    db: Any,
    reply_addr: tuple[str, int] | None = None
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
        reply_addr: Direct reply address (ip, port) from the incoming packet
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
    # Our key_id is the ack_id, their_key_id is the request_id
    # In bootstrap mode: peer_shared_id is NULL, invite_id is set
    # In normal mode: peer_shared_id is set, invite_id is NULL
    # Store reply_addr so we can send sync messages back to this peer
    from_addr_ip = reply_addr[0] if reply_addr else None
    from_addr_port = reply_addr[1] if reply_addr else None

    safedb.execute("""
        INSERT OR REPLACE INTO connections (
            key_id, recorded_by, peer_shared_id, invite_id,
            our_key, their_key_id, their_key,
            created_at, last_handshake_ms, ttl_ms,
            from_addr_ip, from_addr_port
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ack_id, local_peer_id, remote_peer_shared_id, remote_invite_id,
        ack_key, request_id, their_key,
        t_ms, t_ms, CONNECTION_TTL_MS,
        from_addr_ip, from_addr_port
    ))

    # Wrap ack with their symmetric key and queue for delivery
    unsafedb = create_unsafe_db(db)
    ack_blob = store.get(ack_id, unsafedb)

    to_key = {
        'id': crypto.b64decode(request_id),  # Use request_id (their_key_id) as hint
        'key': their_key,
        'type': 'symmetric'
    }

    wrapped = crypto.wrap(ack_blob, to_key, db, random_nonce=True)

    from core import transport
    from events.network.connection_request import get_address_for_peer

    # Use reply_addr if provided (from incoming packet), otherwise look up
    to_addr = reply_addr
    if not to_addr and remote_peer_shared_id:
        to_addr = get_address_for_peer(remote_peer_shared_id, local_peer_id, db)
    if not to_addr and remote_peer_shared_id:
        to_addr = transport.get_peer_address(remote_peer_shared_id)

    from_addr = transport.get_listen_address() or ('127.0.0.1', 0)
    transport.send(wrapped, from_addr, to_addr)
    log.info(f"connection_ack.send_ack_for_request: sent via transport to {to_addr}")

    # Remove from pending requests since we successfully acked
    safedb.execute(
        "DELETE FROM pending_connection_requests WHERE request_id = ? AND recorded_by = ?",
        (request_id, local_peer_id)
    )

    log.info(f"connection_ack.send_ack_for_request: sent ack {ack_id[:20]}... for request {request_id[:20]}...")
