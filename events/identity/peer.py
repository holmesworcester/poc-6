"""Peer event type (local-only identity keypair)."""
from typing import Any

# Registry metadata
EVENT_TYPE = 'peer'
SHAREABLE = False  # Local-only - contains private key material
PROJECTION_TABLE = None  # No projection table (stored in peers table)

# Wire format constants
WIRE_TYPE_CODE = 0x23  # TYPE_PEER
WIRE_PLAINTEXT_SIZE = 344  # PEER_PLAINTEXT_SIZE
PUBKEY_SIZE = 32
PRIVKEY_SIZE = 32

import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.db import create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for peer event type

def encode_plaintext(public_key: bytes, private_key: bytes) -> bytes:
    """Encode a peer payload plaintext (pre-encryption).

    Layout (344 bytes):
    - public_key (32)
    - private_key (32)
    - pad (280)
    """
    wire_format._require_len("public_key", public_key, PUBKEY_SIZE)
    wire_format._require_len("private_key", private_key, PRIVKEY_SIZE)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:32] = public_key
    payload[32:64] = private_key
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a peer payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"peer plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")
    return {
        "public_key": data[0:32],
        "private_key": data[32:64],
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a peer wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    public_key: bytes,
    private_key: bytes,
    created_at_ms: int,
) -> bytes:
    """Encode a complete peer wire event."""
    plaintext = encode_plaintext(public_key=public_key, private_key=private_key)
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_UNSIGNED,
        signer_type=wire_format.SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * wire_format.SIGNER_ID_SIZE,
    )
    payload = wire_format._pad_payload(plaintext)
    signature = b"\x00" * wire_format.SIGNATURE_SIZE
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a peer wire event."""
    header, payload, _signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for peer")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    return {
        "type": "peer",
        "public_key": crypto.b64encode(decoded["public_key"]),
        "private_key": crypto.b64encode(decoded["private_key"]),
        "created_at": header.created_at_ms,
    }


# v2 event specification - no signer, no deps (local-only unsigned event)
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for peer events.

    Peer events are local-only (unsigned, unencrypted) and write to local_peers.
    """
    event_data = ctx.event_data

    public_key = event_data.get('public_key')
    private_key_b64 = event_data.get('private_key')
    created_at = event_data.get('created_at')

    if not public_key or not private_key_b64 or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_peer(public_key, private_key_b64)

    # Decode private key from base64 to bytes for storage
    private_key = crypto.b64decode(private_key_b64)

    writes = (
        WriteOp(
            op='insert',
            table='local_peers',
            values={
                'peer_id': ctx.event_id,
                'public_key': public_key,  # Keep as base64 string
                'private_key': private_key,  # Store as bytes
                'created_at': created_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(t_ms: int, db: Any) -> str:
    """Create a peer (local-only keypair).

    Returns peer_id: the local peer ID for signing operations.

    Note: peer_shared is NOT created here. It is created during network join
    (via invite) so that every peer_shared is invite-signed and linked to a user_id.
    """
    log.info(f"peer.create() creating new peer at t_ms={t_ms}")

    unsafedb = create_unsafe_db(db)

    # Generate keypair
    private_key, public_key = crypto.generate_keypair()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'peer',
        'public_key': crypto.b64encode(public_key),
        'private_key': crypto.b64encode(private_key),
        'created_at': t_ms
    }

    _wire_shadow_peer(event_data['public_key'], event_data['private_key'])

    blob = encode_wire_event(
        public_key=public_key,
        private_key=private_key,
        created_at_ms=t_ms,
    )

    # First store the blob to get the peer_id
    peer_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    log.info(f"peer.create() generated peer_id={peer_id}")

    # Then create recorded wrapper where peer sees itself
    from core import recorded
    recorded_id = recorded.create(peer_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    return peer_id


def _wire_shadow_peer(public_key_b64: str, private_key_b64: str) -> None:
    """Validate peer fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        public_key=crypto.b64decode(public_key_b64),
        private_key=crypto.b64decode(private_key_b64),
    )
    decoded = decode_plaintext(plaintext)
    if decoded["public_key"] != crypto.b64decode(public_key_b64):
        raise ValueError("wire shadow decode public_key mismatch")


def get_private_key(peer_id: str, recorded_by: str, db: Any) -> bytes:
    """Get private key for a peer_id.

    Args:
        peer_id: Peer ID to get private key for
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Private key bytes

    Raises:
        ValueError: If peer not found or recorded_by != peer_id (can only access your own private key)
    """
    # Security: Only allow a peer to access their own private key
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access private key for peer {peer_id}")

    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT private_key FROM local_peers WHERE peer_id = ?", (peer_id,))
    if not row:
        raise ValueError(f"peer not found: {peer_id}")
    return row['private_key']


def get_public_key(peer_id: str, recorded_by: str, db: Any) -> bytes:
    """Get public key for a peer_id from local_peers table.

    Args:
        peer_id: Peer ID to get public key for
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Public key bytes

    Raises:
        ValueError: If peer not found or recorded_by != peer_id (can only access your own peer's public key from local_peers)
    """
    # Security: Only allow a peer to access their own local peer's public key
    # (Public keys for other peers should come from peer_shared events, not local_peers)
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access local peer public key for peer {peer_id}")

    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT public_key FROM local_peers WHERE peer_id = ?", (peer_id,))
    if not row:
        raise ValueError(f"peer not found: {peer_id}")
    # public_key is stored as base64 string in the table
    return crypto.b64decode(row['public_key'])


def list_local(db: Any) -> list[str]:
    """List all local peer IDs we control.

    Returns:
        List of peer_id strings
    """
    unsafedb = create_unsafe_db(db)
    rows = unsafedb.query("SELECT peer_id FROM local_peers")
    return [row['peer_id'] for row in rows]
