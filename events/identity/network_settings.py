"""Network settings event type (shareable) - stores network-level configuration.

Used for star topology sync mode configuration and TreeKEM feature toggle:
- server_relay_peer_shared_id: The relay's peer_shared_id
- server_relay_address: Connection info (e.g., 'relay.example.com:5000')
- sync_mode: 'star' if server relay set, else 'mesh'
- treekem_enabled: True if TreeKEM O(log n) key distribution is enabled
"""

# Registry metadata
EVENT_TYPE = 'network_settings'
SHAREABLE = True  # Network settings sync across network
PROJECTION_TABLE = ('network_settings', 'network_settings_id')

# Wire format constants
WIRE_TYPE_CODE = 0x3B  # TYPE_NETWORK_SETTINGS (new type code)
WIRE_PLAINTEXT_SIZE = 344  # Standard plaintext size

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from events.identity import peer, admin as admin_module
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for network_settings event type

def encode_plaintext(
    network_id: bytes,
    server_relay_peer_shared_id: bytes | None,
    server_relay_address: bytes | None,
    sync_mode: int,  # 0 = mesh, 1 = star
    admin_grant_id: bytes | None,
    treekem_enabled: bool = False,
) -> bytes:
    """Encode a network_settings payload plaintext.

    Layout (344 bytes):
    - network_id (16)
    - server_relay_peer_shared_id (16) - zero-padded if None
    - admin_grant_id (16) - zero-padded if None (bootstrap)
    - sync_mode (1)
    - treekem_enabled (1) - 0 = disabled, 1 = enabled
    - address_len (2)
    - server_relay_address (up to 256 bytes, UTF-8 encoded)
    - pad
    """
    wire_format._require_len("network_id", network_id, 16)
    relay_id_bytes = server_relay_peer_shared_id or (b"\x00" * 16)
    wire_format._require_len("server_relay_peer_shared_id", relay_id_bytes, 16)
    admin_grant_bytes = admin_grant_id or (b"\x00" * 16)
    wire_format._require_len("admin_grant_id", admin_grant_bytes, 16)

    address_bytes = server_relay_address or b""
    if len(address_bytes) > 256:
        raise ValueError("address must be at most 256 bytes")

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = network_id
    payload[16:32] = relay_id_bytes
    payload[32:48] = admin_grant_bytes
    payload[48] = sync_mode & 0xFF
    payload[49] = 1 if treekem_enabled else 0
    payload[50:52] = len(address_bytes).to_bytes(2, 'little')
    payload[52:52 + len(address_bytes)] = address_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a network_settings payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"network_settings plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")
    network_id = data[0:16]
    server_relay_peer_shared_id = data[16:32]
    if server_relay_peer_shared_id == b"\x00" * 16:
        server_relay_peer_shared_id = None
    admin_grant_id = data[32:48]
    if admin_grant_id == b"\x00" * 16:
        admin_grant_id = None
    sync_mode = data[48]
    treekem_enabled = data[49] == 1
    address_len = int.from_bytes(data[50:52], 'little')
    server_relay_address = data[52:52 + address_len] if address_len > 0 else None
    return {
        "network_id": network_id,
        "server_relay_peer_shared_id": server_relay_peer_shared_id,
        "admin_grant_id": admin_grant_id,
        "sync_mode": sync_mode,
        "treekem_enabled": treekem_enabled,
        "server_relay_address": server_relay_address,
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a network_settings wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    network_id_b64: str,
    server_relay_peer_shared_id_b64: str | None,
    server_relay_address: str | None,
    sync_mode: str,  # 'mesh' or 'star'
    signed_by_b64: str,
    signer_type: str,
    admin_grant_id_b64: str | None,
    created_at_ms: int,
    private_key: bytes,
    treekem_enabled: bool = False,
) -> bytes:
    """Encode a complete network_settings wire event."""
    network_id = crypto.b64decode(network_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    server_relay_id = crypto.b64decode(server_relay_peer_shared_id_b64) if server_relay_peer_shared_id_b64 else None
    address_bytes = server_relay_address.encode('utf-8') if server_relay_address else None
    admin_grant_id = crypto.b64decode(admin_grant_id_b64) if admin_grant_id_b64 else None
    sync_mode_int = 1 if sync_mode == 'star' else 0

    plaintext = encode_plaintext(
        network_id=network_id,
        server_relay_peer_shared_id=server_relay_id,
        server_relay_address=address_bytes,
        sync_mode=sync_mode_int,
        admin_grant_id=admin_grant_id,
        treekem_enabled=treekem_enabled,
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
    """Decode a network_settings wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for network_settings")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "network_id": crypto.b64encode(decoded["network_id"]),
        "sync_mode": "star" if decoded["sync_mode"] == 1 else "mesh",
        "treekem_enabled": decoded["treekem_enabled"],
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    if decoded["server_relay_peer_shared_id"]:
        event_data["server_relay_peer_shared_id"] = crypto.b64encode(decoded["server_relay_peer_shared_id"])
    if decoded["server_relay_address"]:
        event_data["server_relay_address"] = decoded["server_relay_address"].decode('utf-8')
    if decoded["admin_grant_id"]:
        event_data["admin_grant"] = crypto.b64encode(decoded["admin_grant_id"])
    return event_data


# v2 event specification - admin-only operation
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


def _wire_shadow_network_settings(
    network_id: str,
    server_relay_peer_shared_id: str | None,
    server_relay_address: str | None,
    sync_mode: str,
    admin_grant: str | None = None,
    treekem_enabled: bool = False,
) -> None:
    """Validate network_settings fields against the fixed-size wire payload layout."""
    relay_id = crypto.b64decode(server_relay_peer_shared_id) if server_relay_peer_shared_id else None
    address_bytes = server_relay_address.encode('utf-8') if server_relay_address else None
    admin_grant_id = crypto.b64decode(admin_grant) if admin_grant else None
    sync_mode_int = 1 if sync_mode == 'star' else 0
    plaintext = encode_plaintext(
        network_id=crypto.b64decode(network_id),
        server_relay_peer_shared_id=relay_id,
        server_relay_address=address_bytes,
        sync_mode=sync_mode_int,
        admin_grant_id=admin_grant_id,
        treekem_enabled=treekem_enabled,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["network_id"] != crypto.b64decode(network_id):
        raise ValueError("wire shadow decode network_id mismatch")


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for network_settings events."""
    event_data = ctx.event_data

    if event_data.get('type') != 'network_settings':
        return ProjectorResult(writes=tuple(), valid_event=False)

    network_id = event_data.get('network_id')
    signed_by = event_data.get('signed_by')
    sync_mode = event_data.get('sync_mode', 'mesh')
    treekem_enabled = event_data.get('treekem_enabled', False)
    server_relay_peer_shared_id = event_data.get('server_relay_peer_shared_id')
    server_relay_address = event_data.get('server_relay_address')
    created_at = event_data.get('created_at')
    admin_grant = event_data.get('admin_grant')

    if not network_id or not signed_by:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_network_settings(network_id, server_relay_peer_shared_id, server_relay_address, sync_mode, admin_grant, treekem_enabled)

    # Verify admin authorization (non-bootstrap requires admin_grant)
    signer = ctx.signer or {}
    is_bootstrap = signed_by == network_id

    if not is_bootstrap:
        if not admin_grant:
            return ProjectorResult(writes=tuple(), valid_event=False)
        signer_user_id = signer.get('user_id')
        grant_row = ctx.deps.get('admin_grant')
        if not grant_row or grant_row.get('user_id') != signer_user_id:
            return ProjectorResult(writes=tuple(), valid_event=False)

    # Each settings event creates a new row; queries use the latest by created_at
    writes = [
        WriteOp(
            op='insert',
            table='network_settings',
            values={
                'network_settings_id': ctx.event_id,
                'network_id': network_id,
                'server_relay_peer_shared_id': server_relay_peer_shared_id,
                'server_relay_address': server_relay_address,
                'sync_mode': sync_mode,
                'treekem_enabled': 1 if treekem_enabled else 0,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    ]

    return ProjectorResult(writes=tuple(writes), valid_event=True)


def set_server_relay(
    network_id: str,
    server_peer_shared_id: str,
    server_address: str,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Set the network's server relay (admin only).

    This configures the network for star topology sync.

    Args:
        network_id: The network to configure
        server_peer_shared_id: The relay's peer_shared_id
        server_address: Connection info (e.g., 'relay.example.com:5000')
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        network_settings_id: The ID of the created network_settings event

    Raises:
        ValueError: If caller is not an admin
    """
    from events.identity import peer_shared as peer_shared_module

    # Get user_id for admin check
    user_id = peer_shared_module.get_user_id(peer_shared_id, peer_id, db)
    if not user_id:
        raise ValueError("Cannot find user_id for peer")

    # Check admin status
    if not admin_module.is_user_admin(user_id, network_id, peer_id, db):
        raise ValueError("Only admins can set server relay")

    # Get admin grant for authorization chain
    admin_grant_id = admin_module.my_grant(user_id, network_id, peer_id, db)
    if not admin_grant_id:
        raise ValueError("No admin grant found")

    # Get peer's private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)

    _wire_shadow_network_settings(network_id, server_peer_shared_id, server_address, 'star', admin_grant_id)

    blob = encode_wire_event(
        network_id_b64=network_id,
        server_relay_peer_shared_id_b64=server_peer_shared_id,
        server_relay_address=server_address,
        sync_mode='star',
        signed_by_b64=peer_shared_id,
        signer_type='peer_shared',
        admin_grant_id_b64=admin_grant_id,
        created_at_ms=t_ms,
        private_key=private_key,
    )
    network_settings_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"network_settings.set_server_relay() created network_settings_id={network_settings_id[:20]}..., "
             f"network_id={network_id[:20]}..., server={server_peer_shared_id[:20]}...")

    return network_settings_id


def get_server_relay(network_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get server relay info for network.

    Args:
        network_id: Network to query
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        Dict with peer_shared_id and address, or None if no server relay
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get the most recent settings for this network (by created_at)
    row = safedb.query_one(
        """SELECT server_relay_peer_shared_id, server_relay_address, sync_mode
           FROM network_settings
           WHERE network_id = ? AND recorded_by = ?
           ORDER BY created_at DESC
           LIMIT 1""",
        (network_id, recorded_by)
    )

    if not row or not row['server_relay_peer_shared_id']:
        return None

    return {
        'peer_shared_id': row['server_relay_peer_shared_id'],
        'address': row['server_relay_address'],
    }


def get_sync_mode(network_id: str, recorded_by: str, db: Any) -> str:
    """Get sync mode: 'star' if server relay set, else 'mesh'.

    Args:
        network_id: Network to query
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        'star' or 'mesh'
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get the most recent settings for this network (by created_at)
    row = safedb.query_one(
        """SELECT sync_mode FROM network_settings
           WHERE network_id = ? AND recorded_by = ?
           ORDER BY created_at DESC
           LIMIT 1""",
        (network_id, recorded_by)
    )

    if not row:
        return 'mesh'  # Default to mesh

    return row['sync_mode']


def is_treekem_enabled(network_id: str, recorded_by: str, db: Any) -> bool:
    """Check if TreeKEM is enabled for a network.

    Args:
        network_id: Network to query
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if TreeKEM is enabled, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get the most recent settings for this network (by created_at)
    row = safedb.query_one(
        """SELECT treekem_enabled FROM network_settings
           WHERE network_id = ? AND recorded_by = ?
           ORDER BY created_at DESC
           LIMIT 1""",
        (network_id, recorded_by)
    )

    if not row:
        return False  # Default to disabled

    return row['treekem_enabled'] == 1


def set_treekem_enabled(
    network_id: str,
    enabled: bool,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Enable or disable TreeKEM for a network (admin only).

    Args:
        network_id: The network to configure
        enabled: True to enable TreeKEM, False to disable
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        network_settings_id: The ID of the created network_settings event

    Raises:
        ValueError: If caller is not an admin
    """
    from events.identity import peer_shared as peer_shared_module

    # Get user_id for admin check
    user_id = peer_shared_module.get_user_id(peer_shared_id, peer_id, db)
    if not user_id:
        raise ValueError("Cannot find user_id for peer")

    # Check admin status
    if not admin_module.is_user_admin(user_id, network_id, peer_id, db):
        raise ValueError("Only admins can configure TreeKEM")

    # Get admin grant for authorization chain
    admin_grant_id = admin_module.my_grant(user_id, network_id, peer_id, db)
    if not admin_grant_id:
        raise ValueError("No admin grant found")

    # Get current settings to preserve other values
    safedb = create_safe_db(db, recorded_by=peer_id)
    current = safedb.query_one(
        """SELECT server_relay_peer_shared_id, server_relay_address, sync_mode
           FROM network_settings
           WHERE network_id = ? AND recorded_by = ?
           ORDER BY created_at DESC
           LIMIT 1""",
        (network_id, peer_id)
    )

    server_relay_id = current['server_relay_peer_shared_id'] if current else None
    server_relay_address = current['server_relay_address'] if current else None
    sync_mode = current['sync_mode'] if current else 'mesh'

    # Get peer's private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)

    _wire_shadow_network_settings(network_id, server_relay_id, server_relay_address, sync_mode, admin_grant_id, enabled)

    blob = encode_wire_event(
        network_id_b64=network_id,
        server_relay_peer_shared_id_b64=server_relay_id,
        server_relay_address=server_relay_address,
        sync_mode=sync_mode,
        signed_by_b64=peer_shared_id,
        signer_type='peer_shared',
        admin_grant_id_b64=admin_grant_id,
        created_at_ms=t_ms,
        private_key=private_key,
        treekem_enabled=enabled,
    )
    network_settings_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"network_settings.set_treekem_enabled() created network_settings_id={network_settings_id[:20]}..., "
             f"network_id={network_id[:20]}..., treekem_enabled={enabled}")

    return network_settings_id
