"""Admin event type (shareable, plaintext) - grants admin status to a user.

This is a first-class event type, NOT a group. Admin status is granted by:
- Bootstrap: signed_by=network_id (verified with network_pubkey)
- Ongoing: signed_by=peer_shared_id (verified with peer.pubkey + admin_grant chain)
"""

# Registry metadata
EVENT_TYPE = 'admin'
SHAREABLE = True  # Admin grants sync across network
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x29  # TYPE_ADMIN
WIRE_PLAINTEXT_SIZE = 344  # ADMIN_PLAINTEXT_SIZE

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for admin event type

def encode_plaintext(
    user_id: bytes,
    network_id: bytes,
    admin_grant_id: bytes | None,
) -> bytes:
    """Encode an admin payload plaintext.

    Layout (344 bytes):
    - user_id (16)
    - network_id (16)
    - admin_grant_id (16)
    - pad
    """
    wire_format._require_len("user_id", user_id, 16)
    wire_format._require_len("network_id", network_id, 16)
    admin_grant_bytes = admin_grant_id or (b"\x00" * 16)
    wire_format._require_len("admin_grant_id", admin_grant_bytes, 16)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = user_id
    payload[16:32] = network_id
    payload[32:48] = admin_grant_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode an admin payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"admin plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")
    user_id = data[0:16]
    network_id = data[16:32]
    admin_grant_id = data[32:48]
    if admin_grant_id == b"\x00" * 16:
        admin_grant_id = None
    return {"user_id": user_id, "network_id": network_id, "admin_grant_id": admin_grant_id}


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is an admin wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    user_id_b64: str,
    network_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    admin_grant_id_b64: str | None,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a complete admin wire event."""
    user_id = crypto.b64decode(user_id_b64)
    network_id = crypto.b64decode(network_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    admin_grant_id = crypto.b64decode(admin_grant_id_b64) if admin_grant_id_b64 else None
    plaintext = encode_plaintext(
        user_id=user_id,
        network_id=network_id,
        admin_grant_id=admin_grant_id,
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
    """Decode an admin wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for admin")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "user_id": crypto.b64encode(decoded["user_id"]),
        "network_id": crypto.b64encode(decoded["network_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    if decoded["admin_grant_id"]:
        event_data["admin_grant"] = crypto.b64encode(decoded["admin_grant_id"])
    return event_data


EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'network': {
            'source': 'table',
            'table': 'networks',
            'key': 'network_id',
            'fields': ['network_id'],
        },
    },
    'optional': {
        'admin_grant': {
            'source': 'table',
            'table': 'admins',
            'key': 'admin_id',
            'key_from': 'admin_grant',
            'fields': ['admin_id', 'user_id'],
            'required_if_present': True,
        },
    },
    'cascade_on_delete': [],
}


def _wire_shadow_admin(user_id: str, network_id: str, admin_grant: str | None) -> None:
    """Validate admin fields against the fixed-size wire payload layout."""
    admin_grant_bytes = crypto.b64decode(admin_grant) if admin_grant else None
    plaintext = encode_plaintext(
        user_id=crypto.b64decode(user_id),
        network_id=crypto.b64decode(network_id),
        admin_grant_id=admin_grant_bytes,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["user_id"] != crypto.b64decode(user_id):
        raise ValueError("wire shadow decode user_id mismatch")

def create(
    user_id: str,
    network_id: str,
    signed_by: str,
    signer_private_key: bytes,
    t_ms: int,
    peer_id: str,
    db: Any,
    admin_grant: str | None = None
) -> str:
    """Create an admin event granting admin status to a user.

    Args:
        user_id: The user being granted admin
        network_id: The network this admin grant is for
        signed_by: Either network_id (bootstrap) or peer_shared_id (ongoing)
        signer_private_key: Private key corresponding to signed_by
        t_ms: Timestamp
        peer_id: Local peer ID (for recording)
        db: Database connection
        admin_grant: Prior admin_id for authorization chain (None for bootstrap)

    Returns:
        admin_id: The ID of the created admin event
    """
    _wire_shadow_admin(user_id, network_id, admin_grant)

    blob = encode_wire_event(
        user_id_b64=user_id,
        network_id_b64=network_id,
        signed_by_b64=signed_by,
        signer_type='network' if signed_by == network_id else 'peer_shared',
        admin_grant_id_b64=admin_grant,
        created_at_ms=t_ms,
        private_key=signer_private_key,
    )
    admin_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"admin.create() created admin grant: admin_id={admin_id[:20]}..., "
             f"user_id={user_id[:20]}..., network_id={network_id[:20]}..., "
             f"signed_by={signed_by[:20]}...")

    return admin_id


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for admin events."""
    event_data = ctx.event_data

    if event_data.get('type') != 'admin':
        return ProjectorResult(writes=tuple(), valid_event=False)

    signed_by = event_data.get('signed_by')
    network_id = event_data.get('network_id')
    user_id = event_data.get('user_id')
    admin_grant = event_data.get('admin_grant')

    if not signed_by or not network_id or not user_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_admin(user_id, network_id, admin_grant)

    signer = ctx.signer or {}
    signer_type = signer.get('type')
    is_bootstrap = signed_by == network_id

    if is_bootstrap:
        if signer_type and signer_type != 'network':
            return ProjectorResult(writes=tuple(), valid_event=False)
    else:
        if not admin_grant:
            return ProjectorResult(writes=tuple(), valid_event=False)
        if signer_type and signer_type != 'peer_shared':
            return ProjectorResult(writes=tuple(), valid_event=False)
        signer_user_id = signer.get('user_id')
        if not signer_user_id:
            return ProjectorResult(writes=tuple(), valid_event=False)
        grant_row = ctx.deps.get('admin_grant')
        if not grant_row or grant_row.get('user_id') != signer_user_id:
            return ProjectorResult(writes=tuple(), valid_event=False)

    writes = [
        WriteOp(
            op='insert',
            table='admins',
            values={
                'admin_id': ctx.event_id,
                'network_id': network_id,
                'user_id': user_id,
                'signed_by': signed_by,
                'admin_grant': admin_grant,
                'created_at': event_data.get('created_at'),
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    ]

    if is_bootstrap:
        writes.append(
            WriteOp(
                op='update',
                table='networks',
                values={'creator_user_id': user_id},
                where={
                    'network_id': network_id,
                    'recorded_by': ctx.recorded_by,
                    'creator_user_id': '',
                },
            )
        )

    return ProjectorResult(writes=tuple(writes), valid_event=True)


def is_user_admin(user_id: str, network_id: str, recorded_by: str, db: Any) -> bool:
    """Check if a user has admin status in a network.

    Args:
        user_id: The user to check
        network_id: The network to check admin status for
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if user is an admin, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    admin_row = safedb.query_one(
        "SELECT 1 FROM admins WHERE user_id = ? AND network_id = ? AND recorded_by = ?",
        (user_id, network_id, recorded_by)
    )

    return admin_row is not None


def my_grant(user_id: str, network_id: str, recorded_by: str, db: Any) -> str | None:
    """Get the admin_id that granted admin to a user.

    Used for creating admin_grant chain when granting admin to others.

    Args:
        user_id: The admin user
        network_id: The network
        recorded_by: Peer perspective
        db: Database connection

    Returns:
        admin_id if found, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    admin_row = safedb.query_one(
        "SELECT admin_id FROM admins WHERE user_id = ? AND network_id = ? AND recorded_by = ?",
        (user_id, network_id, recorded_by)
    )

    return admin_row['admin_id'] if admin_row else None
