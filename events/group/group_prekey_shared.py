"""Prekey shared event type (shareable public prekey)."""

# Registry metadata
EVENT_TYPE = 'group_prekey_shared'
SHAREABLE = True  # Public prekeys sync for key sealing
PROJECTION_TABLE = ('group_prekeys_shared', 'group_prekey_shared_id')

# Wire format constants
WIRE_TYPE_CODE = 0x15  # TYPE_GROUP_PREKEY_SHARED
WIRE_PLAINTEXT_SIZE = 344  # GROUP_PREKEY_SHARED_PLAINTEXT_SIZE

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from events.identity import peer
from events.group import group_prekey
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for group_prekey_shared event type

def encode_plaintext(
    group_prekey_id: bytes,
    peer_id: bytes,
    public_key: bytes,
) -> bytes:
    """Encode a group_prekey_shared payload plaintext.

    Layout (344 bytes):
    - group_prekey_id (16)
    - peer_id (16)
    - public_key (32)
    - pad
    """
    wire_format._require_len("group_prekey_id", group_prekey_id, 16)
    wire_format._require_len("peer_id", peer_id, 16)
    wire_format._require_len("public_key", public_key, wire_format.PUBKEY_SIZE)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = group_prekey_id
    payload[16:32] = peer_id
    payload[32:64] = public_key
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group_prekey_shared payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"group_prekey_shared plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {
        "group_prekey_id": data[0:16],
        "peer_id": data[16:32],
        "public_key": data[32:64],
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a group_prekey_shared wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    group_prekey_id_b64: str,
    peer_id_b64: str,
    public_key_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a complete group_prekey_shared wire event."""
    group_prekey_id = crypto.b64decode(group_prekey_id_b64)
    peer_id = crypto.b64decode(peer_id_b64)
    public_key = crypto.b64decode(public_key_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_plaintext(
        group_prekey_id=group_prekey_id,
        peer_id=peer_id,
        public_key=public_key,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=0,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = wire_format._pad_payload(plaintext)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a group_prekey_shared wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for group_prekey_shared")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    return {
        "type": EVENT_TYPE,
        "group_prekey_id": crypto.b64encode(decoded["group_prekey_id"]),
        "peer_id": crypto.b64encode(decoded["peer_id"]),
        "public_key": crypto.b64encode(decoded["public_key"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }


# v2 event specification - signed by peer_shared, no deps
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for group_prekey_shared events."""
    event_data = ctx.event_data

    group_prekey_id = event_data.get('group_prekey_id')
    peer_id = event_data.get('peer_id')
    public_key_b64 = event_data.get('public_key')
    created_at = event_data.get('created_at')

    if not all([group_prekey_id, peer_id, public_key_b64, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_group_prekey_shared(group_prekey_id, peer_id, public_key_b64)

    public_key = crypto.b64decode(public_key_b64)

    writes = (
        WriteOp(
            op='insert',
            table='group_prekeys_shared',
            values={
                'group_prekey_shared_id': ctx.event_id,
                'group_prekey_id': group_prekey_id,
                'peer_id': peer_id,
                'public_key': public_key,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_group_prekey_shared(group_prekey_id: str, peer_id: str, public_key_b64: str) -> None:
    """Validate group_prekey_shared fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        group_prekey_id=crypto.b64decode(group_prekey_id),
        peer_id=crypto.b64decode(peer_id),
        public_key=crypto.b64decode(public_key_b64),
    )
    decoded = decode_plaintext(plaintext)
    if decoded["group_prekey_id"] != crypto.b64decode(group_prekey_id):
        raise ValueError("wire shadow decode group_prekey_id mismatch")


def create(prekey_id: str, peer_id: str, peer_shared_id: str,
           t_ms: int, db: Any,
           group_id: str | None = None, key_id: str | None = None,
           user_id: str | None = None,
           wrap_key_data: dict | None = None) -> str:
    """Create a shareable group_prekey_shared event from a local group prekey.

    Context parameters (group_id, key_id, user_id) are optional and used for
    documentation/logging purposes. The event itself is signed, not encrypted,
    so no context is required for the wire format.

    Args:
        prekey_id: Local group_prekey event ID (to get public key from)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection
        group_id: Optional group context (for logging/documentation)
        key_id: Optional key reference (for logging/documentation)
        user_id: Optional user context (for logging/documentation)
        wrap_key_data: Optional key dict for wrapping (unused - event is signed not encrypted)

    Returns:
        group_prekey_shared_id: The stored group_prekey_shared event ID
    """
    # Context is optional - used for logging only, not in the actual event

    log.info(f"group_prekey_shared.create() creating group_prekey_shared for prekey_id={prekey_id}, t_ms={t_ms}")

    # Get public key from local prekey event
    prekey_blob = store.get(prekey_id, db)
    if not prekey_blob:
        raise ValueError(f"prekey not found: {prekey_id}")

    if not group_prekey.is_wire_envelope(prekey_blob):
        raise ValueError("group_prekey_shared requires wire group_prekey event")
    prekey_data = group_prekey.decode_wire_event(prekey_blob)
    prekey_public_b64 = prekey_data['public_key']

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)

    blob = encode_wire_event(
        group_prekey_id_b64=prekey_id,
        peer_id_b64=peer_shared_id,
        public_key_b64=prekey_public_b64,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )

    # Store event with recorded wrapper and projection
    group_prekey_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_prekey_shared.create() created group_prekey_shared_id={group_prekey_shared_id}")
    return group_prekey_shared_id
