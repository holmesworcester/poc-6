"""Server relay event type (shareable, plaintext) - marks a peer as a relay server.

Server relays are special peers that:
1. Sync events with all members (store and forward)
2. Have peer identity (peer_shared)
3. Do NOT have user identity (no user_id)
4. Do NOT receive group keys (cannot decrypt messages)
5. Cannot create admin events (no admin_grant possible)
6. Cannot send messages (no user identity)

This enables star topology sync where all peers sync through a central relay,
scaling from O(n^2) negotiations/tick to O(n) negotiations/tick.
"""

# Registry metadata
EVENT_TYPE = 'server_relay'
SHAREABLE = True  # Server relay identity syncs across network
PROJECTION_TABLE = ('server_relays', 'server_relay_id')

# Wire format constants
WIRE_TYPE_CODE = 0x3A  # TYPE_SERVER_RELAY (new type code)
WIRE_PLAINTEXT_SIZE = 344  # Standard plaintext size

from typing import Any
import logging
import json
import base64
from core import crypto
from core import store
from core import wire_format
from events.identity import peer, peer_shared, network
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for server_relay event type

def encode_plaintext(
    peer_shared_id: bytes,
    network_id: bytes,
    address: bytes | None,
) -> bytes:
    """Encode a server_relay payload plaintext.

    Layout (344 bytes):
    - peer_shared_id (16)
    - network_id (16)
    - address_len (2)
    - address (up to 256 bytes, UTF-8 encoded)
    - pad
    """
    wire_format._require_len("peer_shared_id", peer_shared_id, 16)
    wire_format._require_len("network_id", network_id, 16)

    address_bytes = address or b""
    if len(address_bytes) > 256:
        raise ValueError("address must be at most 256 bytes")

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = peer_shared_id
    payload[16:32] = network_id
    payload[32:34] = len(address_bytes).to_bytes(2, 'little')
    payload[34:34 + len(address_bytes)] = address_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a server_relay payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"server_relay plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")
    peer_shared_id = data[0:16]
    network_id = data[16:32]
    address_len = int.from_bytes(data[32:34], 'little')
    address = data[34:34 + address_len] if address_len > 0 else None
    return {
        "peer_shared_id": peer_shared_id,
        "network_id": network_id,
        "address": address,
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a server_relay wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    peer_shared_id_b64: str,
    network_id_b64: str,
    address: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a complete server_relay wire event."""
    peer_shared_id = crypto.b64decode(peer_shared_id_b64)
    network_id = crypto.b64decode(network_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    address_bytes = address.encode('utf-8') if address else None

    plaintext = encode_plaintext(
        peer_shared_id=peer_shared_id,
        network_id=network_id,
        address=address_bytes,
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
    """Decode a server_relay wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for server_relay")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "peer_shared_id": crypto.b64encode(decoded["peer_shared_id"]),
        "network_id": crypto.b64encode(decoded["network_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    if decoded["address"]:
        event_data["address"] = decoded["address"].decode('utf-8')
    return event_data


# v2 event specification - signed by peer_shared or network
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
        'peer_shared': {
            'source': 'table',
            'table': 'peers_shared',
            'key': 'peer_shared_id',
            'key_from': 'peer_shared_id',
            'fields': ['peer_shared_id', 'public_key'],
        },
    },
    'optional': {},
    'cascade_on_delete': [],
}


def _wire_shadow_server_relay(peer_shared_id: str, network_id: str, address: str | None) -> None:
    """Validate server_relay fields against the fixed-size wire payload layout."""
    address_bytes = address.encode('utf-8') if address else None
    plaintext = encode_plaintext(
        peer_shared_id=crypto.b64decode(peer_shared_id),
        network_id=crypto.b64decode(network_id),
        address=address_bytes,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["peer_shared_id"] != crypto.b64decode(peer_shared_id):
        raise ValueError("wire shadow decode peer_shared_id mismatch")


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for server_relay events."""
    event_data = ctx.event_data

    if event_data.get('type') != 'server_relay':
        return ProjectorResult(writes=tuple(), valid_event=False)

    peer_shared_id = event_data.get('peer_shared_id')
    network_id = event_data.get('network_id')
    signed_by = event_data.get('signed_by')
    address = event_data.get('address')
    created_at = event_data.get('created_at')

    if not peer_shared_id or not network_id or not signed_by:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_server_relay(peer_shared_id, network_id, address)

    writes = [
        WriteOp(
            op='insert',
            table='server_relays',
            values={
                'server_relay_id': ctx.event_id,
                'peer_shared_id': peer_shared_id,
                'network_id': network_id,
                'address': address,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    ]

    return ProjectorResult(writes=tuple(writes), valid_event=True)


def create(
    peer_id: str,
    peer_shared_id: str,
    network_id: str,
    t_ms: int,
    db: Any,
    address: str | None = None,
) -> str:
    """Create server_relay event marking this peer as a relay.

    Server relays have peer identity but NO user identity.
    They can sync events but cannot decrypt messages or take admin actions.

    Args:
        peer_id: Local peer ID
        peer_shared_id: Public peer ID of the relay
        network_id: Network this relay serves
        t_ms: Timestamp
        db: Database connection
        address: Optional connection address (e.g., 'relay.example.com:5000')

    Returns:
        server_relay_id: The ID of the created server_relay event
    """
    # Get peer's private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)

    _wire_shadow_server_relay(peer_shared_id, network_id, address)

    blob = encode_wire_event(
        peer_shared_id_b64=peer_shared_id,
        network_id_b64=network_id,
        address=address,
        signed_by_b64=peer_shared_id,
        signer_type='peer_shared',
        created_at_ms=t_ms,
        private_key=private_key,
    )
    server_relay_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"server_relay.create() created server_relay_id={server_relay_id[:20]}..., "
             f"peer_shared_id={peer_shared_id[:20]}..., network_id={network_id[:20]}...")

    return server_relay_id


def join(peer_id: str, invite_link: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Join network as server relay (not as user).

    Creates peer_shared but NOT user event. Server relays cannot decrypt
    messages or take admin actions.

    Args:
        peer_id: Local peer ID
        invite_link: Invite link with mode='server'
        t_ms: Timestamp
        db: Database connection

    Returns:
        Dict with:
        - peer_id: Local peer ID
        - peer_shared_id: Public peer ID
        - network_id: Network ID from invite
        - server_relay_id: The server_relay event ID
        (Note: NO user_id - server relays don't have user identity)
    """
    # Parse invite link
    if not invite_link.startswith("quiet://server/"):
        raise ValueError("Server relay invite must start with quiet://server/")

    code = invite_link.replace("quiet://server/", "")

    # Decode base64-urlsafe JSON
    try:
        padding = '=' * (4 - len(code) % 4)
        link_json = base64.urlsafe_b64decode(code + padding).decode()
        link_data = json.loads(link_json)
    except Exception as e:
        raise ValueError(f"Failed to parse server invite link: {e}")

    network_id = link_data.get('network_id')
    invite_private_key = crypto.b64decode(link_data.get('invite_private_key', ''))
    inviter_peer_shared_id = link_data.get('inviter_peer_shared_id')
    server_address = link_data.get('server_address')

    if not network_id:
        raise ValueError("Server invite must contain network_id")

    # Get or verify peer exists
    # Verify the peer exists
    unsafedb = create_unsafe_db(db)
    peer_row = unsafedb.query_one(
        "SELECT peer_id FROM local_peers WHERE peer_id = ?",
        (peer_id,)
    )
    if not peer_row:
        raise ValueError(f"Peer {peer_id} not found")

    # Create invite_accepted event for the server invite
    from events.identity import invite_accepted as invite_accepted_module
    invite_accepted_id = invite_accepted_module.create(
        invite_link_data=link_data,
        peer_id=peer_id,
        t_ms=t_ms,
        db=db
    )

    # Create peer_shared event (signed by invite)
    # Note: No user event is created - server relays don't have user identity
    invite_id = link_data.get('invite_id')
    peer_shared_id = peer_shared.create(
        peer_id=peer_id,
        t_ms=t_ms,
        db=db,
        invite_id=invite_id,
        invite_private_key=invite_private_key,
        device_name="Server Relay",
    )

    # Create server_relay event marking this peer as a relay
    server_relay_id = create(
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        network_id=network_id,
        t_ms=t_ms,
        db=db,
        address=server_address,
    )

    # Create connection prekeys for sync
    from events.network import connection_prekey
    connection_prekey.generate_batch(peer_id, 5, t_ms, db)  # Generate 5 prekeys

    log.info(f"server_relay.join() completed: peer_shared_id={peer_shared_id[:20]}..., "
             f"network_id={network_id[:20]}..., server_relay_id={server_relay_id[:20]}...")

    return {
        'peer_id': peer_id,
        'peer_shared_id': peer_shared_id,
        'network_id': network_id,
        'server_relay_id': server_relay_id,
        # Note: NO user_id - server relays don't have user identity
    }


def is_server_relay(peer_shared_id: str, recorded_by: str, db: Any) -> bool:
    """Check if a peer is a server relay.

    Args:
        peer_shared_id: Public peer ID to check
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if peer is a server relay, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    relay_row = safedb.query_one(
        "SELECT 1 FROM server_relays WHERE peer_shared_id = ? AND recorded_by = ?",
        (peer_shared_id, recorded_by)
    )

    return relay_row is not None


def get_server_relay(peer_shared_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get server relay info for a peer.

    Args:
        peer_shared_id: Public peer ID
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        Dict with server_relay_id, peer_shared_id, network_id, address or None
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    row = safedb.query_one(
        "SELECT * FROM server_relays WHERE peer_shared_id = ? AND recorded_by = ?",
        (peer_shared_id, recorded_by)
    )

    if not row:
        return None

    return {
        'server_relay_id': row['server_relay_id'],
        'peer_shared_id': row['peer_shared_id'],
        'network_id': row['network_id'],
        'address': row['address'],
        'created_at': row['created_at'],
    }
