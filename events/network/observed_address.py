"""Observed address event type (peer-announced endpoint observations).

A peer announces observations about another peer's public endpoint.
This supports Byzantine fault tolerance: multiple peers can attest to the same address.

Usage:
  - Alice observes Bob at 203.0.113.5:42000 (from Bob's sync packet)
  - Alice creates address event announcing this observation
  - Charlie receives Alice's observation and learns Bob's endpoint
"""

# Registry metadata
EVENT_TYPE = 'observed_address'
SHAREABLE = True  # Address observations sync for peer discovery
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x35  # TYPE_OBSERVED_ADDRESS
WIRE_PLAINTEXT_SIZE = 344  # OBSERVED_ADDRESS_PLAINTEXT_SIZE

from typing import Any, Optional
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for observed_address event type

def encode_plaintext(observed_peer_id: bytes, ip: str, port: int) -> bytes:
    """Encode an observed_address payload plaintext.

    Layout (344 bytes):
    - observed_peer_id (16)
    - ip (16, IPv6 or IPv4-mapped)
    - port (u16)
    - pad
    """
    wire_format._require_len("observed_peer_id", observed_peer_id, 16)
    if port < 0 or port > 0xFFFF:
        raise ValueError("port must fit in u16")
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = observed_peer_id
    payload[16:32] = wire_format._encode_ip16(ip)
    struct.pack_into("<H", payload, 32, port)
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode an observed_address payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"observed_address plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    observed_peer_id = data[0:16]
    ip = wire_format._decode_ip16(data[16:32])
    (port,) = struct.unpack_from("<H", data, 32)
    return {"observed_peer_id": observed_peer_id, "ip": ip, "port": port}


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is an observed_address wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    observed_peer_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    ip: str,
    port: int,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a complete observed_address wire event."""
    observed_peer_id = crypto.b64decode(observed_peer_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_plaintext(
        observed_peer_id=observed_peer_id,
        ip=ip,
        port=port,
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
    """Decode an observed_address wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for observed_address")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    return {
        "type": EVENT_TYPE,
        "observed_peer_id": crypto.b64encode(decoded["observed_peer_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "ip": decoded["ip"],
        "port": decoded["port"],
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }


def _wire_shadow_observed_address(observed_peer_id: str, ip: str, port: int) -> None:
    """Validate observed_address fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        observed_peer_id=crypto.b64decode(observed_peer_id),
        ip=ip,
        port=port,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["ip"] != ip or decoded["port"] != port:
        raise ValueError("wire shadow decode observed_address mismatch")


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
    """Pure projector for observed_address events.

    Observed addresses are signed plaintext events recording peer endpoints.
    """
    event_data = ctx.event_data

    observed_peer_id = event_data.get('observed_peer_id')
    signed_by = event_data.get('signed_by')
    ip = event_data.get('ip')
    port = event_data.get('port')
    created_at = event_data.get('created_at')

    if not all([observed_peer_id, signed_by, ip, port is not None, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_observed_address(observed_peer_id, ip, port)

    writes = (
        WriteOp(
            op='insert',
            table='network_addresses',
            values={
                'address_id': ctx.event_id,
                'observed_peer_id': observed_peer_id,
                'signed_by': signed_by,
                'ip': ip,
                'port': port,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(
    observed_peer_id: str,
    peer_id: str,
    peer_shared_id: str,
    ip: str,
    port: int,
    t_ms: int,
    db: Any
) -> str:
    """Create address event announcing observation of a peer's endpoint.

    Args:
        observed_peer_id: The peer whose endpoint was observed
        peer_id: Local peer_id of the observer (for signing/storage)
        peer_shared_id: peer_shared_id of the observer (for signed_by field)
        ip: Observed IP address
        port: Observed port number
        t_ms: Timestamp
        db: Database connection

    Returns:
        address_id: Event ID of the created address event
    """
    log.info(
        f"observed_address.create() {peer_shared_id[:20]}... observed "
        f"{observed_peer_id[:20]}... at {ip}:{port}"
    )

    # Sign the event
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)
    _wire_shadow_observed_address(observed_peer_id, ip, port)

    blob = encode_wire_event(
        observed_peer_id_b64=observed_peer_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        ip=ip,
        port=port,
        created_at_ms=t_ms,
        private_key=private_key,
    )
    address_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"observed_address.create() created address_id={address_id[:20]}...")
    return address_id


def get_addresses(peer_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """Get all known addresses for a peer from local perspective.

    Args:
        peer_id: The peer whose addresses to look up
        recorded_by: Local peer ID doing the lookup
        db: Database connection

    Returns:
        List of dicts with keys: address_id, ip, port, signed_by, created_at
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    rows = safedb.query(
        """SELECT address_id, ip, port, signed_by, created_at
           FROM network_addresses
           WHERE observed_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id, recorded_by)
    )

    return [
        {
            'address_id': row['address_id'],
            'ip': row['ip'],
            'port': row['port'],
            'signed_by': row['signed_by'],
            'created_at': row['created_at']
        }
        for row in rows
    ]


def get_latest_address(peer_id: str, recorded_by: str, db: Any) -> Optional[tuple[str, int]]:
    """Get the most recent known address for a peer.

    Args:
        peer_id: The peer whose address to look up
        recorded_by: Local peer ID doing the lookup
        db: Database connection

    Returns:
        (ip, port) tuple or None if no addresses known
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    row = safedb.query_one(
        """SELECT ip, port FROM network_addresses
           WHERE observed_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC LIMIT 1""",
        (peer_id, recorded_by)
    )

    if row:
        return (row['ip'], row['port'])
    return None
