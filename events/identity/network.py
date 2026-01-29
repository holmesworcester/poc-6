"""Network event type - self-signed root of trust for a network."""

# Registry metadata
EVENT_TYPE = 'network'
SHAREABLE = True  # Network root of trust syncs to all peers
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x27  # TYPE_NETWORK
WIRE_PLAINTEXT_SIZE = 344  # NETWORK_PLAINTEXT_SIZE

from typing import Any
import struct
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp


EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'network_pubkey',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
    # Trust anchor enforcement: network events require a trust anchor before projection
    # Trust anchors are inserted by invite_accepted for both creators and joiners
    'trust_anchor': {
        'table': 'trust_anchors',
        'key': 'network_id',
        'key_from': '@event_id',  # The network_id IS the event_id
    },
}

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for network event type

def encode_plaintext(network_pubkey: bytes) -> bytes:
    """Encode a network payload plaintext (pre-encryption).

    Layout (344 bytes):
    - network_pubkey (32)
    - pad
    """
    wire_format._require_len("network_pubkey", network_pubkey, wire_format.PUBKEY_SIZE)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:32] = network_pubkey
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a network payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"network plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")
    return {"network_pubkey": data[0:32]}


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a network wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    network_pubkey: bytes,
    created_at_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a complete network wire event."""
    plaintext = encode_plaintext(network_pubkey=network_pubkey)
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=0,
        signer_type=wire_format.SIGNER_NETWORK,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * wire_format.SIGNER_ID_SIZE,
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = wire_format._pad_payload(plaintext)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a network wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for network")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    return {
        "type": EVENT_TYPE,
        "network_pubkey": crypto.b64encode(decoded["network_pubkey"]),
        "signer_type": "network",
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }


def _wire_shadow_network(network_pubkey_b64: str) -> None:
    """Validate network fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        network_pubkey=crypto.b64decode(network_pubkey_b64)
    )
    decoded = decode_plaintext(plaintext)
    if decoded["network_pubkey"] != crypto.b64decode(network_pubkey_b64):
        raise ValueError("wire shadow decode network_pubkey mismatch")


def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a self-signed network event (root of trust).

    The network event is the first shared event in bootstrap. It's self-signed
    using its own keypair (like a root CA certificate). Groups, channels, and
    other content are created AFTER the user has a peer_shared identity.

    Network names are transmitted via encrypted network_name_update events
    to protect privacy from NETWORK ACTIVE ATTACKER.

    Args:
        peer_id: Local peer ID (for recording, not signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        tuple: (network_id, network_private_key)
            - network_id: The stored network event ID (hash of event)
            - network_private_key: Private key for signing bootstrap invites and admin_grant
    """
    log.info(f"network.create() creating self-signed network at t_ms={t_ms}")

    # Generate network's own keypair (network is self-signed)
    network_private_key, network_public_key = crypto.generate_keypair()

    blob = encode_wire_event(
        network_pubkey=network_public_key,
        created_at_ms=t_ms,
        private_key=network_private_key,
    )

    # Store the event - it will be blocked until invite_accepted creates trust anchor
    # Trust anchors come from invite_accepted.project() for both creators and joiners
    network_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"network.create() created self-signed network_id={network_id}")
    return network_id, network_private_key


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for network events."""
    event_data = ctx.event_data
    network_pubkey = event_data.get('network_pubkey')
    created_at = event_data.get('created_at')

    if not network_pubkey or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_network(network_pubkey)

    writes = (
        WriteOp(
            op='insert',
            table='networks',
            values={
                'network_id': ctx.event_id,
                'creator_user_id': '',
                'network_pubkey': network_pubkey,
                'signed_by': ctx.event_id,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def get_all_users_group_id(network_id: str, recorded_by: str, db: Any) -> str:
    """Get all_users group ID for a network.

    The all_users group is cryptographically identified by being signed by the
    network itself (signed_by = network_id). This is the principled way to
    discover which group is the network's main membership group.

    Args:
        network_id: Network ID
        recorded_by: Peer ID querying
        db: Database connection

    Returns:
        All users group ID

    Raises:
        ValueError: If all_users group not found for network
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Query by signature - the all_users group is signed by the network
    group = safedb.query_one(
        "SELECT group_id FROM groups WHERE signed_by = ? AND recorded_by = ?",
        (network_id, recorded_by)
    )

    if not group:
        raise ValueError(f"Network-signed all_users group not found for network {network_id}")

    return group['group_id']


def get_public_key(network_id: str, recorded_by: str, db: Any) -> bytes:
    """Get network public key for signature verification.

    Used to verify signed_by=network_id on bootstrap invites.

    Args:
        network_id: Network ID
        recorded_by: Peer ID querying
        db: Database connection

    Returns:
        Network public key as bytes

    Raises:
        ValueError: If network not found or no network_pubkey
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    network = safedb.query_one(
        "SELECT network_pubkey FROM networks WHERE network_id = ? AND recorded_by = ?",
        (network_id, recorded_by)
    )

    if not network:
        raise ValueError(f"Network {network_id} not found")

    network_pubkey = network['network_pubkey']
    if not network_pubkey:
        raise ValueError(f"Network {network_id} has no network_pubkey")

    return crypto.b64decode(network_pubkey)


def get_public_key_from_store(network_id: str, db: Any) -> bytes | None:
    """Get network public key by reading directly from the network blob in store.

    This is the conventional approach for projectors that need a network's public key
    during cascade processing - reads from the raw blob rather than projection tables.
    This avoids timing issues where an event is in valid_events but not yet projected.

    Args:
        network_id: The network event ID
        db: Database connection

    Returns:
        Public key bytes, or None if blob not found or not a network event
    """
    unsafedb = create_unsafe_db(db)
    blob = store.get(network_id, unsafedb)
    if not blob:
        log.warning(f"get_public_key_from_store() blob not found: {network_id[:20]}...")
        return None

    try:
        if not is_wire_envelope(blob):
            log.warning(f"get_public_key_from_store() expected wire network event: {network_id[:20]}...")
            return None
        event_data = decode_wire_event(blob)
        if event_data.get('type') != 'network':
            log.warning(f"get_public_key_from_store() not a network event: {network_id[:20]}...")
            return None
        network_pubkey_b64 = event_data.get('network_pubkey')
        if not network_pubkey_b64:
            log.warning(f"get_public_key_from_store() no network_pubkey in blob: {network_id[:20]}...")
            return None
        return crypto.b64decode(network_pubkey_b64)
    except Exception as e:
        log.warning(f"get_public_key_from_store() failed to parse blob: {network_id[:20]}... {e}")
        return None


def get_network_id(recorded_by: str, db: Any) -> str | None:
    """Get the network_id for a peer's view.

    Args:
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        network_id string if found, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT network_id FROM networks WHERE recorded_by = ? LIMIT 1",
        (recorded_by,)
    )
    return row['network_id'] if row else None


def get_for_peer(peer_id: str, recorded_by: str, db: Any) -> dict | None:
    """Get network info for a peer.

    Args:
        peer_id: Peer ID to get network for
        recorded_by: Peer ID querying
        db: Database connection

    Returns:
        Dict with network_id (use get_all_users_group_id/get_admin_group_id for groups)
        or None if peer not in a network
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get user_id for this peer from peers_shared (user→peer relationship stored there)
    peer_row = safedb.query_one(
        "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
        (peer_id, recorded_by)
    )

    if not peer_row or not peer_row['user_id']:
        return None

    # Get network_id from users table using user_id
    user_row = safedb.query_one(
        "SELECT network_id FROM users WHERE user_id = ? AND recorded_by = ?",
        (peer_row['user_id'], recorded_by)
    )

    if not user_row or not user_row['network_id']:
        return None

    network_id = user_row['network_id']

    # Get network details (no longer includes group IDs - query groups table for those)
    network = safedb.query_one(
        "SELECT network_id, network_pubkey, signed_by, created_at FROM networks WHERE network_id = ? AND recorded_by = ?",
        (network_id, recorded_by)
    )

    return network


def get_network_id_for_peer(recorded_by: str, db: Any) -> str | None:
    """Return the network_id for this peer's local view, if available."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT network_id FROM networks WHERE recorded_by = ? LIMIT 1",
        (recorded_by,),
    )
    return row["network_id"] if row else None
