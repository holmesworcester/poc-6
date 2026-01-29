"""Self-announced address event type (peer's own network address for direct communication)."""

# Registry metadata
EVENT_TYPE = 'self_address'
SHAREABLE = True  # Addresses sync to enable peer discovery
PROJECTION_TABLE = ('addresses', 'address_id')

# Wire format constants
WIRE_TYPE_CODE = 0x34  # TYPE_SELF_ADDRESS
WIRE_PLAINTEXT_SIZE = 344  # SELF_ADDRESS_PLAINTEXT_SIZE

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from events.identity import peer
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for self_address event type

def encode_plaintext(peer_id: bytes, ip: str, port: int) -> bytes:
    """Encode a self_address payload plaintext (pre-encryption).

    Layout (344 bytes):
    - peer_id (16)
    - ip (16) - IPv6 or IPv4-mapped-IPv6
    - port (u16)
    - pad
    """
    wire_format._require_len("peer_id", peer_id, 16)
    if port < 0 or port > 0xFFFF:
        raise ValueError("port must fit in u16")
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = peer_id
    payload[16:32] = wire_format._encode_ip16(ip)
    struct.pack_into("<H", payload, 32, port)
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a self_address payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"self_address plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    peer_id = data[0:16]
    ip = wire_format._decode_ip16(data[16:32])
    (port,) = struct.unpack_from("<H", data, 32)
    return {"peer_id": peer_id, "ip": ip, "port": port}


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a self_address wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    peer_id_b64: str,
    ip: str,
    port: int,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a complete self_address wire event."""
    peer_id = crypto.b64decode(peer_id_b64)
    plaintext = encode_plaintext(peer_id=peer_id, ip=ip, port=port)
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=0,
        signer_type=wire_format.SIGNER_PEER_SHARED,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", peer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = wire_format._pad_payload(plaintext)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a self_address wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for self_address")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    if decoded["peer_id"] != header.signer_id:
        raise ValueError("peer_id does not match signer_id")
    return {
        "type": EVENT_TYPE,
        "peer_id": crypto.b64encode(decoded["peer_id"]),
        "ip": decoded["ip"],
        "port": decoded["port"],
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }


def _wire_shadow_self_address(peer_shared_id: str, ip: str, port: int) -> None:
    """Validate self_address fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        peer_id=crypto.b64decode(peer_shared_id),
        ip=ip,
        port=port,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["ip"] != ip or decoded["port"] != port:
        raise ValueError("wire shadow decode self_address mismatch")

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
    """Pure projector for self_address events."""
    event_data = ctx.event_data

    peer_shared_id = event_data.get('peer_id')  # Note: field is 'peer_id' not 'peer_shared_id'
    ip = event_data.get('ip')
    port = event_data.get('port')
    created_at = event_data.get('created_at')

    if not all([peer_shared_id, ip, port is not None, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_self_address(peer_shared_id, ip, port)

    writes = (
        WriteOp(
            op='insert',
            table='addresses',
            values={
                'address_id': ctx.event_id,
                'peer_shared_id': peer_shared_id,
                'ip': ip,
                'port': port,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(peer_id: str, peer_shared_id: str, ip: str, port: int, t_ms: int, db: Any) -> str:
    """Create an address event for a peer.

    Args:
        peer_id: Local peer ID (creator)
        peer_shared_id: Public peer shared ID
        ip: IP address
        port: Port number
        t_ms: Timestamp
        db: Database connection

    Returns:
        address_id: Event ID of the created address event
    """
    log.info(f"self_address.create() creating address for peer_shared_id={peer_shared_id[:20]}..., ip={ip}, port={port}")

    # Get peer's private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)

    _wire_shadow_self_address(peer_shared_id, ip, port)

    blob = encode_wire_event(
        peer_id_b64=peer_shared_id,
        ip=ip,
        port=port,
        created_at_ms=t_ms,
        private_key=private_key,
    )
    address_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"self_address.create() created address_id={address_id[:20]}...")
    return address_id


def get_latest(peer_shared_id: str, recorded_by: str, db: Any) -> tuple[str, int] | None:
    """Get the most recent self-announced address for a peer.

    Args:
        peer_shared_id: The peer's shared ID
        recorded_by: Local peer ID doing the lookup
        db: Database connection

    Returns:
        (ip, port) tuple or None if no addresses known
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    row = safedb.query_one(
        """SELECT ip, port FROM addresses
           WHERE peer_shared_id = ? AND recorded_by = ?
           ORDER BY created_at DESC LIMIT 1""",
        (peer_shared_id, recorded_by)
    )

    if row:
        return (row['ip'], row['port'])
    return None


def announce_for_all_peers(t_ms: int, db: Any) -> dict[str, Any]:
    """Announce self-address for all local peers.

    Checks current address from network layer and announces if changed
    or if no previous announcement exists.

    Args:
        t_ms: Current timestamp in milliseconds
        db: Database connection

    Returns:
        Dict with stats: announced (count), skipped (count), errors (list)
    """
    from core.db import create_unsafe_db

    stats = {'announced': 0, 'skipped': 0, 'errors': []}

    # Get network engine if available
    try:
        from simulator import network
        engine = network.get_engine()
    except ImportError:
        engine = None

    unsafedb = create_unsafe_db(db)
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']

        # Get peer_shared_id
        safedb = create_safe_db(db, recorded_by=peer_id)
        peer_self_row = safedb.query_one(
            "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
            (peer_id, peer_id)
        )

        if not peer_self_row:
            log.debug(f"self_address.announce: skipping peer {peer_id[:20]}... (no peer_shared_id)")
            stats['skipped'] += 1
            continue

        peer_shared_id = peer_self_row['peer_shared_id']

        # Get current address from network layer
        if engine:
            endpoint = engine.get_endpoint(peer_id)
            if endpoint:
                current_ip = endpoint.public_ip
                current_port = endpoint.public_port
            else:
                log.debug(f"self_address.announce: skipping peer {peer_id[:20]}... (no endpoint)")
                stats['skipped'] += 1
                continue
        else:
            # No network layer - skip
            log.debug(f"self_address.announce: skipping peer {peer_id[:20]}... (no network layer)")
            stats['skipped'] += 1
            continue

        # Check if address changed
        latest = get_latest(peer_shared_id, peer_id, db)
        if latest and latest[0] == current_ip and latest[1] == current_port:
            log.debug(f"self_address.announce: skipping peer {peer_id[:20]}... (address unchanged)")
            stats['skipped'] += 1
            continue

        # Announce new address
        try:
            address_id = create(peer_id, peer_shared_id, current_ip, current_port, t_ms, db)
            project(address_id, peer_id, t_ms, db)
            stats['announced'] += 1
            log.info(f"self_address.announce: announced {current_ip}:{current_port} for peer {peer_id[:20]}...")
        except Exception as e:
            stats['errors'].append(f"peer {peer_id[:20]}...: {e}")
            log.warning(f"self_address.announce: error for peer {peer_id[:20]}...: {e}")

    return stats
