"""TreeKEM pubkey event type (subjective keypairs at tree positions).

Local-only event that stores Ed25519 keypairs for tree nodes on the peer's path.
These keypairs are used for O(log n) key distribution on member removal.
"""

# Registry metadata
EVENT_TYPE = 'treekem_pubkey'
SHAREABLE = False  # Local-only - contains private key material
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x40  # TYPE_TREEKEM_PUBKEY
WIRE_PLAINTEXT_SIZE = 344  # Standard plaintext size
PUBKEY_SIZE = 32
PRIVKEY_SIZE = 32

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core import treekem
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions

def encode_plaintext(
    group_id: bytes,
    depth: int,
    path_prefix: bytes,
    public_key: bytes,
    private_key: bytes,
) -> bytes:
    """Encode a treekem_pubkey payload plaintext.

    Layout (344 bytes):
    - group_id (16)
    - depth (2)
    - path_prefix_len (2)
    - path_prefix (up to 16 bytes)
    - public_key (32)
    - private_key (32)
    - pad
    """
    wire_format._require_len("group_id", group_id, 16)
    wire_format._require_len("public_key", public_key, PUBKEY_SIZE)
    wire_format._require_len("private_key", private_key, PRIVKEY_SIZE)

    if len(path_prefix) > 16:
        raise ValueError("path_prefix must be at most 16 bytes")

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = group_id
    payload[16:18] = depth.to_bytes(2, 'little')
    payload[18:20] = len(path_prefix).to_bytes(2, 'little')
    payload[20:20 + len(path_prefix)] = path_prefix
    payload[36:68] = public_key
    payload[68:100] = private_key
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a treekem_pubkey payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"treekem_pubkey plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")

    group_id = data[0:16]
    depth = int.from_bytes(data[16:18], 'little')
    prefix_len = int.from_bytes(data[18:20], 'little')
    path_prefix = data[20:20 + prefix_len]
    public_key = data[36:68]
    private_key = data[68:100]

    return {
        "group_id": group_id,
        "depth": depth,
        "path_prefix": path_prefix,
        "public_key": public_key,
        "private_key": private_key,
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a treekem_pubkey wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    group_id: bytes,
    depth: int,
    path_prefix: bytes,
    public_key: bytes,
    private_key: bytes,
    created_at_ms: int,
) -> bytes:
    """Encode a complete treekem_pubkey wire event."""
    plaintext = encode_plaintext(
        group_id=group_id,
        depth=depth,
        path_prefix=path_prefix,
        public_key=public_key,
        private_key=private_key,
    )
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
    """Decode a treekem_pubkey wire event."""
    header, payload, _signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for treekem_pubkey")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    return {
        "type": EVENT_TYPE,
        "group_id": crypto.b64encode(decoded["group_id"]),
        "depth": decoded["depth"],
        "path_prefix": crypto.b64encode(decoded["path_prefix"]),
        "public_key": crypto.b64encode(decoded["public_key"]),
        "private_key": crypto.b64encode(decoded["private_key"]),
        "created_at": header.created_at_ms,
    }


# v2 event specification - no signer, no deps (local-only deterministic event)
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for treekem_pubkey events."""
    event_data = ctx.event_data

    if event_data.get('type') != EVENT_TYPE:
        return ProjectorResult(writes=tuple(), valid_event=False)

    group_id = event_data.get('group_id')
    depth = event_data.get('depth')
    path_prefix_b64 = event_data.get('path_prefix')
    public_key_b64 = event_data.get('public_key')
    private_key_b64 = event_data.get('private_key')

    if not group_id or depth is None or not public_key_b64 or not private_key_b64:
        return ProjectorResult(writes=tuple(), valid_event=False)

    path_prefix = crypto.b64decode(path_prefix_b64) if path_prefix_b64 else b''
    public_key = crypto.b64decode(public_key_b64)
    private_key = crypto.b64decode(private_key_b64)

    # Calculate TTL
    created_at = ctx.recorded_at
    ttl_ms = created_at + treekem.TREEKEM_PUBKEY_TTL_MS

    writes = (
        WriteOp(
            op='insert',
            table='treekem_pubkeys',
            values={
                'treekem_pubkey_id': ctx.event_id,
                'group_id': group_id,
                'depth': depth,
                'path_prefix': path_prefix,
                'public_key': public_key,
                'private_key': private_key,
                'created_at': created_at,
                'ttl_ms': ttl_ms,
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
    peer_id: str,
    t_ms: int,
    db: Any,
) -> tuple[str, bytes, bytes]:
    """Create a treekem_pubkey event with a new keypair.

    Generates Ed25519 keypair for the specified tree node.

    Args:
        group_id: The group this pubkey is for
        depth: Tree depth (0 = root)
        path_prefix: Path prefix bytes for the node
        peer_id: Local peer ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        Tuple of (pubkey_id, public_key, private_key)
    """
    log.info(f"treekem_pubkey.create() group={group_id[:20]}..., depth={depth}, peer={peer_id[:20]}...")

    # Generate Ed25519 keypair
    private_key, public_key = crypto.generate_keypair()

    blob = encode_wire_event(
        group_id=crypto.b64decode(group_id),
        depth=depth,
        path_prefix=path_prefix,
        public_key=public_key,
        private_key=private_key,
        created_at_ms=t_ms,
    )

    pubkey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"treekem_pubkey.create() created pubkey_id={pubkey_id[:20]}...")

    return pubkey_id, public_key, private_key


def get_pubkey_for_node(
    group_id: str,
    depth: int,
    path_prefix: bytes,
    peer_id: str,
    db: Any,
) -> dict[str, Any] | None:
    """Get the local keypair for a specific tree node.

    Args:
        group_id: The group
        depth: Tree depth
        path_prefix: Node path prefix
        peer_id: Local peer ID
        db: Database connection

    Returns:
        Dict with pubkey_id, public_key, private_key or None if not found
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    row = safedb.query_one(
        """SELECT treekem_pubkey_id, public_key, private_key, ttl_ms
           FROM treekem_pubkeys
           WHERE group_id = ? AND depth = ? AND path_prefix = ? AND recorded_by = ?
           ORDER BY created_at DESC
           LIMIT 1""",
        (group_id, depth, path_prefix, peer_id)
    )

    if not row:
        return None

    return {
        'pubkey_id': row['treekem_pubkey_id'],
        'public_key': row['public_key'],
        'private_key': row['private_key'],
        'ttl_ms': row['ttl_ms'],
    }


def get_or_create_path_pubkeys(
    group_id: str,
    peer_shared_id: str,
    peer_id: str,
    t_ms: int,
    db: Any,
) -> list[tuple[int, bytes, str, bytes]]:
    """Get or create pubkeys for all nodes on peer's path to root.

    Args:
        group_id: The group
        peer_shared_id: Public peer ID (for path computation)
        peer_id: Local peer ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        List of (depth, path_prefix, pubkey_id, public_key) tuples
    """
    path = treekem.path_to_root(peer_shared_id)
    result = []

    for i, (depth, path_prefix) in enumerate(path):
        existing = get_pubkey_for_node(group_id, depth, path_prefix, peer_id, db)

        if existing and existing['ttl_ms'] > t_ms:
            # Use existing valid pubkey
            result.append((depth, path_prefix, existing['pubkey_id'], existing['public_key']))
        else:
            # Create new pubkey
            pubkey_id, public_key, _private_key = create(
                group_id=group_id,
                depth=depth,
                path_prefix=path_prefix,
                peer_id=peer_id,
                t_ms=t_ms + i,
                db=db,
            )
            result.append((depth, path_prefix, pubkey_id, public_key))

    return result


def list_for_group(group_id: str, peer_id: str, t_ms: int, db: Any) -> list[dict[str, Any]]:
    """List all TreeKEM pubkeys for a group.

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
        """SELECT treekem_pubkey_id, depth, path_prefix, public_key, created_at, ttl_ms
           FROM treekem_pubkeys
           WHERE group_id = ? AND recorded_by = ?
           ORDER BY depth, path_prefix""",
        (group_id, peer_id)
    )

    return [
        {
            'pubkey_id': row['treekem_pubkey_id'],
            'depth': row['depth'],
            'path_prefix': row['path_prefix'],
            'public_key': row['public_key'],
            'created_at': row['created_at'],
            'ttl_ms': row['ttl_ms'],
            'valid': row['ttl_ms'] > t_ms,
        }
        for row in rows
    ]
