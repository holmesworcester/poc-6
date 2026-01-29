"""Message reaction deletion event type - handles deletion of emoji reactions.

This is the deletion counterpart to message_reaction.
When a reaction is removed, a message_reaction_deletion event is created.
"""
from typing import Any
import logging
import struct
from core import crypto
from core import wire_format
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Event type declarations for auto-discovery
EVENT_TYPE = 'message_reaction_deletion'
SHAREABLE = True  # Sync deletions to other peers
PROJECTION_TABLE = ('message_reaction_deletions', 'deletion_id')

# Wire format constants
WIRE_TYPE_CODE = 0x06  # TYPE_MESSAGE_REACTION_DELETION
WIRE_PLAINTEXT_SIZE = 344  # MESSAGE_REACTION_DELETION_PLAINTEXT_SIZE


# Wire format functions - encode/decode for message_reaction_deletion event type

def encode_plaintext(reaction_id: bytes) -> bytes:
    """Encode a message_reaction_deletion payload plaintext (pre-encryption)."""
    wire_format._require_len("reaction_id", reaction_id, 16)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = reaction_id
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a message_reaction_deletion payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            "message_reaction_deletion plaintext must be "
            f"{WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {"reaction_id": data[0:16]}


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt message_reaction_deletion plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"message_reaction_deletion plaintext must be {WIRE_PLAINTEXT_SIZE} bytes"
        )
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("message_reaction_deletion payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != WIRE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for message_reaction_deletion payload")
    payload = key_id + nonce + ciphertext
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to message_reaction_deletion plaintext."""
    if key_data.get("type") != "symmetric":
        raise ValueError("message_reaction_deletion payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a message_reaction_deletion wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    reaction_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete message_reaction_deletion wire event."""
    reaction_id = crypto.b64decode(reaction_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_plaintext(reaction_id=reaction_id)
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_payload(plaintext, key_data)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode a message_reaction_deletion wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        return None, []
    if header.flags & wire_format.FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_payload(payload, key_data)
    else:
        plaintext = payload[:WIRE_PLAINTEXT_SIZE]

    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "reaction_id": crypto.b64encode(decoded["reaction_id"]),
        "deleted_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data, []


# v2 event specification - signed by peer_shared, encrypted
EVENT_SPEC = {
    'encrypted': True,
    'signer': {
        'id_field': 'deleted_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'reaction': {
            'source': 'table',
            'table': 'message_reactions',
            'key': 'reaction_id',
            'fields': ['reaction_id', 'reactor_id'],
        },
    },
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for message_reaction_deletion events."""
    event_data = ctx.event_data

    reaction_id = event_data.get('reaction_id')
    deleted_by = event_data.get('deleted_by')
    created_at = event_data.get('created_at')

    if not all([reaction_id, deleted_by, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_message_reaction_deletion(reaction_id)

    reaction_row = ctx.deps.get('reaction') or {}
    signer = ctx.signer or {}
    signer_user_id = signer.get('user_id')
    reactor_id = reaction_row.get('reactor_id')
    if not signer_user_id or not reactor_id or signer_user_id != reactor_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='delete',
            table='message_reactions',
            values={},
            where={
                'reaction_id': reaction_id,
                'recorded_by': ctx.recorded_by,
            },
        ),
        WriteOp(
            op='insert',
            table='message_reaction_deletions',
            values={
                'deletion_id': ctx.event_id,
                'reaction_id': reaction_id,
                'deleted_by': deleted_by,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_message_reaction_deletion(reaction_id: str) -> None:
    """Validate message_reaction_deletion fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        reaction_id=crypto.b64decode(reaction_id)
    )
    decoded = decode_plaintext(plaintext)
    if decoded["reaction_id"] != crypto.b64decode(reaction_id):
        raise ValueError("wire shadow decode reaction_id mismatch")
