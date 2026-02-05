"""TreeKEM pubkey shared event type (shareable pubkeys for tree nodes).

Shareable event that distributes public keys for tree nodes to other group members.
Recipients store these to enable O(log n) key distribution on member removal.
"""

# Registry metadata
EVENT_TYPE = 'treekem_pubkey_shared'
SHAREABLE = True  # Pubkeys sync across group
PROJECTION_TABLE = ('treekem_pubkeys_shared', 'treekem_pubkey_shared_id')

# Wire format constants
WIRE_TYPE_CODE = 0x41  # TYPE_TREEKEM_PUBKEY_SHARED
WIRE_PLAINTEXT_SIZE = 344  # Standard plaintext size
PUBKEY_SIZE = 32

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core import treekem
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp
from events.identity import peer

log = logging.getLogger(__name__)


# Wire format functions

def encode_plaintext(
    group_id: bytes,
    depth: int,
    path_prefix: bytes,
    public_key: bytes,
    owner_peer_shared_id: bytes,
) -> bytes:
    """Encode a treekem_pubkey_shared payload plaintext.

    Layout (344 bytes):
    - group_id (16)
    - depth (2)
    - path_prefix_len (2)
    - path_prefix (up to 16 bytes)
    - public_key (32)
    - owner_peer_shared_id (16)
    - pad
    """
    wire_format._require_len("group_id", group_id, 16)
    wire_format._require_len("public_key", public_key, PUBKEY_SIZE)
    wire_format._require_len("owner_peer_shared_id", owner_peer_shared_id, 16)

    if len(path_prefix) > 16:
        raise ValueError("path_prefix must be at most 16 bytes")

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = group_id
    payload[16:18] = depth.to_bytes(2, 'little')
    payload[18:20] = len(path_prefix).to_bytes(2, 'little')
    payload[20:20 + len(path_prefix)] = path_prefix
    payload[36:68] = public_key
    payload[68:84] = owner_peer_shared_id
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a treekem_pubkey_shared payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"treekem_pubkey_shared plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")

    group_id = data[0:16]
    depth = int.from_bytes(data[16:18], 'little')
    prefix_len = int.from_bytes(data[18:20], 'little')
    path_prefix = data[20:20 + prefix_len]
    public_key = data[36:68]
    owner_peer_shared_id = data[68:84]

    return {
        "group_id": group_id,
        "depth": depth,
        "path_prefix": path_prefix,
        "public_key": public_key,
        "owner_peer_shared_id": owner_peer_shared_id,
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a treekem_pubkey_shared wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    group_id_b64: str,
    depth: int,
    path_prefix: bytes,
    public_key: bytes,
    owner_peer_shared_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    ttl_ms: int,
    private_key: bytes,
) -> bytes:
    """Encode a complete treekem_pubkey_shared wire event."""
    group_id = crypto.b64decode(group_id_b64)
    owner_id = crypto.b64decode(owner_peer_shared_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)

    plaintext = encode_plaintext(
        group_id=group_id,
        depth=depth,
        path_prefix=path_prefix,
        public_key=public_key,
        owner_peer_shared_id=owner_id,
    )
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
    """Decode a treekem_pubkey_shared wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for treekem_pubkey_shared")

    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)

    return {
        "type": EVENT_TYPE,
        "group_id": crypto.b64encode(decoded["group_id"]),
        "depth": decoded["depth"],
        "path_prefix": crypto.b64encode(decoded["path_prefix"]),
        "public_key": crypto.b64encode(decoded["public_key"]),
        "owner_peer_shared_id": crypto.b64encode(decoded["owner_peer_shared_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "ttl_ms": header.ttl_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }


# v2 event specification
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
    """Pure projector for treekem_pubkey_shared events."""
    event_data = ctx.event_data

    if event_data.get('type') != EVENT_TYPE:
        return ProjectorResult(writes=tuple(), valid_event=False)

    group_id = event_data.get('group_id')
    depth = event_data.get('depth')
    path_prefix_b64 = event_data.get('path_prefix')
    public_key_b64 = event_data.get('public_key')
    owner_peer_shared_id = event_data.get('owner_peer_shared_id')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')
    ttl_ms = event_data.get('ttl_ms', 0)

    if not group_id or depth is None or not public_key_b64 or not owner_peer_shared_id or not signed_by:
        return ProjectorResult(writes=tuple(), valid_event=False)

    path_prefix = crypto.b64decode(path_prefix_b64) if path_prefix_b64 else b''
    public_key = crypto.b64decode(public_key_b64)

    # Calculate absolute TTL if relative
    if ttl_ms > 0 and ttl_ms < created_at:
        # TTL is relative, convert to absolute
        absolute_ttl = created_at + ttl_ms
    else:
        absolute_ttl = ttl_ms if ttl_ms > 0 else created_at + treekem.TREEKEM_PUBKEY_TTL_MS

    writes = (
        WriteOp(
            op='insert',
            table='treekem_pubkeys_shared',
            values={
                'treekem_pubkey_shared_id': ctx.event_id,
                'group_id': group_id,
                'depth': depth,
                'path_prefix': path_prefix,
                'public_key': public_key,
                'owner_peer_shared_id': owner_peer_shared_id,
                'created_at': created_at,
                'ttl_ms': absolute_ttl,
                'signed_by': signed_by,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(
    group_id: str,
    depth: int,
    path_prefix: bytes,
    public_key: bytes,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create a treekem_pubkey_shared event to share a pubkey.

    Args:
        group_id: The group this pubkey is for
        depth: Tree depth (0 = root)
        path_prefix: Path prefix bytes for the node
        public_key: The public key to share
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (owner of the pubkey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        pubkey_shared_id
    """
    log.info(f"treekem_pubkey_shared.create() group={group_id[:20]}..., depth={depth}, peer={peer_shared_id[:20]}...")

    # Get private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Calculate TTL
    ttl_ms = t_ms + treekem.TREEKEM_PUBKEY_TTL_MS

    blob = encode_wire_event(
        group_id_b64=group_id,
        depth=depth,
        path_prefix=path_prefix,
        public_key=public_key,
        owner_peer_shared_id_b64=peer_shared_id,
        signed_by_b64=peer_shared_id,
        signer_type='peer_shared',
        created_at_ms=t_ms,
        ttl_ms=ttl_ms,
        private_key=private_key,
    )

    pubkey_shared_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"treekem_pubkey_shared.create() created pubkey_shared_id={pubkey_shared_id[:20]}...")

    return pubkey_shared_id


def get_valid_nodes_for_group(
    group_id: str,
    peer_id: str,
    t_ms: int,
    db: Any,
) -> set[tuple[int, bytes]]:
    """Get set of (depth, path_prefix) for all nodes with valid pubkeys.

    Args:
        group_id: The group
        peer_id: Local peer ID
        t_ms: Current timestamp for TTL check
        db: Database connection

    Returns:
        Set of (depth, path_prefix) tuples with valid pubkeys
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    rows = safedb.query(
        """SELECT DISTINCT depth, path_prefix
           FROM treekem_pubkeys_shared
           WHERE group_id = ? AND recorded_by = ? AND ttl_ms > ?""",
        (group_id, peer_id, t_ms)
    )

    return {(row['depth'], row['path_prefix']) for row in rows}


def get_pubkey_for_node(
    group_id: str,
    depth: int,
    path_prefix: bytes,
    peer_id: str,
    t_ms: int,
    db: Any,
) -> dict[str, Any] | None:
    """Get the most recent valid pubkey for a tree node.

    Args:
        group_id: The group
        depth: Tree depth
        path_prefix: Node path prefix
        peer_id: Local peer ID
        t_ms: Current timestamp for TTL check
        db: Database connection

    Returns:
        Dict with pubkey info or None if no valid pubkey
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    row = safedb.query_one(
        """SELECT treekem_pubkey_shared_id, public_key, owner_peer_shared_id, ttl_ms
           FROM treekem_pubkeys_shared
           WHERE group_id = ? AND depth = ? AND path_prefix = ? AND recorded_by = ? AND ttl_ms > ?
           ORDER BY created_at DESC
           LIMIT 1""",
        (group_id, depth, path_prefix, peer_id, t_ms)
    )

    if not row:
        return None

    return {
        'pubkey_shared_id': row['treekem_pubkey_shared_id'],
        'public_key': row['public_key'],
        'owner_peer_shared_id': row['owner_peer_shared_id'],
        'ttl_ms': row['ttl_ms'],
    }


def list_for_group(group_id: str, peer_id: str, t_ms: int, db: Any) -> list[dict[str, Any]]:
    """List all received TreeKEM pubkeys for a group.

    Args:
        group_id: The group
        peer_id: Local peer ID
        t_ms: Current timestamp for TTL check
        db: Database connection

    Returns:
        List of pubkey dicts with node info and validity status
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    rows = safedb.query(
        """SELECT treekem_pubkey_shared_id, depth, path_prefix, public_key,
                  owner_peer_shared_id, created_at, ttl_ms
           FROM treekem_pubkeys_shared
           WHERE group_id = ? AND recorded_by = ?
           ORDER BY depth, path_prefix""",
        (group_id, peer_id)
    )

    return [
        {
            'pubkey_shared_id': row['treekem_pubkey_shared_id'],
            'depth': row['depth'],
            'path_prefix': row['path_prefix'],
            'public_key': row['public_key'],
            'owner_peer_shared_id': row['owner_peer_shared_id'],
            'created_at': row['created_at'],
            'ttl_ms': row['ttl_ms'],
            'valid': row['ttl_ms'] > t_ms,
        }
        for row in rows
    ]


def share_path_pubkeys(
    group_id: str,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> list[str]:
    """Share all pubkeys on peer's path to root.

    Creates or refreshes local pubkeys and shares them via treekem_pubkey_shared events.

    Args:
        group_id: The group
        peer_id: Local peer ID
        peer_shared_id: Public peer ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        List of pubkey_shared_ids created
    """
    from events.group import treekem_pubkey

    # Get or create local pubkeys for path
    path_pubkeys = treekem_pubkey.get_or_create_path_pubkeys(
        group_id=group_id,
        peer_shared_id=peer_shared_id,
        peer_id=peer_id,
        t_ms=t_ms,
        db=db,
    )

    # Share each pubkey
    shared_ids = []
    for i, (depth, path_prefix, _pubkey_id, public_key) in enumerate(path_pubkeys):
        shared_id = create(
            group_id=group_id,
            depth=depth,
            path_prefix=path_prefix,
            public_key=public_key,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms + len(path_pubkeys) + i,  # Offset to ensure unique timestamps
            db=db,
        )
        shared_ids.append(shared_id)

    return shared_ids
