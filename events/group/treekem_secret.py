"""TreeKEM treekem_secret event type (local-only path secret for copath key distribution).

This stores path secrets for TreeKEM O(log n) key distribution. Path secrets are
local-only, deterministic events that store symmetric key material at specific
tree positions (depth + path_prefix).

Pattern: treekem_secret (local, SHAREABLE=False) + treekem_secret_shared (shareable, SHAREABLE=True)
Similar to: secret + secret_shared
"""

# Registry metadata
EVENT_TYPE = 'treekem_secret'
SHAREABLE = False  # Local-only - contains symmetric key material
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Event specification - no signer, local-only deterministic event
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for treekem_secret events.

    treekem_secret events are local-only deterministic events (created_at=0 in blob).
    Uses recorded_at as created_at.
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'treekem_secret':
        return ProjectorResult(writes=tuple(), valid_event=False)

    depth = event_data.get('depth')
    path_prefix_b64 = event_data.get('path_prefix')
    key_b64 = event_data.get('key')

    if depth is None or path_prefix_b64 is None or not key_b64:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Validate depth
    if depth < 0 or depth > 255:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Wire shadow validation
    try:
        path_prefix = crypto.b64decode(path_prefix_b64)
        key_bytes = crypto.b64decode(key_b64)
        _wire_shadow_treekem_secret(depth, path_prefix, key_bytes)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    if len(key_bytes) != 32:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='treekem_secrets',
            values={
                'treekem_secret_id': ctx.event_id,
                'depth': depth,
                'path_prefix': path_prefix,
                'key': key_bytes,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_treekem_secret(depth: int, path_prefix: bytes, key: bytes) -> None:
    """Validate treekem_secret fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_treekem_secret_plaintext(
        depth=depth, path_prefix=path_prefix, key=key
    )
    decoded = wire_format.decode_treekem_secret_plaintext(plaintext)

    if decoded["depth"] != depth:
        raise ValueError("wire shadow decode depth mismatch")

    # Path prefix is padded to 16 bytes in wire format
    expected_prefix = (path_prefix + b"\x00" * 16)[:16]
    if decoded["path_prefix"] != expected_prefix:
        raise ValueError("wire shadow decode path_prefix mismatch")

    if decoded["key"] != key:
        raise ValueError("wire shadow decode key mismatch")


def create(
    depth: int,
    path_prefix: bytes,
    peer_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create a new treekem_secret with a fresh symmetric key at a tree position.

    Args:
        depth: Tree depth (0 = root)
        path_prefix: Path prefix bytes for this tree node
        peer_id: Local peer ID (owner)
        t_ms: Timestamp
        db: Database connection

    Returns:
        treekem_secret_id: The event ID
    """
    log.info(f"treekem_secret.create() creating path secret at depth={depth} for peer_id={peer_id[:20]}...")

    # Generate symmetric key
    key = crypto.generate_secret()

    # Create DETERMINISTIC event blob - only type, depth, path_prefix, key
    _wire_shadow_treekem_secret(depth, path_prefix, key)

    blob = wire_format.encode_treekem_secret_wire_event(
        depth=depth,
        path_prefix=path_prefix,
        key=key,
        created_at_ms=0,  # Deterministic
    )

    # Store event with recorded wrapper and projection
    treekem_secret_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"treekem_secret.create() created treekem_secret_id={treekem_secret_id[:20]}...")

    return treekem_secret_id


def create_with_material(
    depth: int,
    path_prefix: bytes,
    key_material: bytes,
    peer_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create treekem_secret event with provided key material.

    Creates a DETERMINISTIC treekem_secret event from the key material.
    Same key_material = same treekem_secret_id on all peers.

    Args:
        depth: Tree depth (0 = root)
        path_prefix: Path prefix bytes for this tree node
        key_material: The symmetric key bytes (32 bytes)
        peer_id: Peer ID that owns this key
        t_ms: Timestamp (used for recorded_at, NOT in the blob)
        db: Database connection

    Returns:
        treekem_secret_id: The deterministic event ID
    """
    log.info(f"treekem_secret.create_with_material() creating path secret at depth={depth} for peer_id={peer_id[:20]}...")

    if len(key_material) != 32:
        raise ValueError(f"key_material must be 32 bytes, got {len(key_material)}")

    # Create DETERMINISTIC event blob
    _wire_shadow_treekem_secret(depth, path_prefix, key_material)

    blob = wire_format.encode_treekem_secret_wire_event(
        depth=depth,
        path_prefix=path_prefix,
        key=key_material,
        created_at_ms=0,  # Deterministic
    )

    treekem_secret_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"treekem_secret.create_with_material() created treekem_secret_id={treekem_secret_id[:20]}...")
    return treekem_secret_id


def get_key_bytes(treekem_secret_id: str, recorded_by: str, db: Any) -> bytes | None:
    """Get raw key bytes for a treekem_secret.

    Args:
        treekem_secret_id: The treekem_secret event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Key bytes or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT key FROM treekem_secrets WHERE treekem_secret_id = ? AND recorded_by = ?",
        (treekem_secret_id, recorded_by)
    )
    return row['key'] if row else None


def get_secret(treekem_secret_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get full treekem_secret record.

    Args:
        treekem_secret_id: The treekem_secret event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Secret record dict or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_one(
        """SELECT treekem_secret_id, depth, path_prefix, key, recorded_at
           FROM treekem_secrets WHERE treekem_secret_id = ? AND recorded_by = ?""",
        (treekem_secret_id, recorded_by)
    )


def get_secret_at_position(
    depth: int,
    path_prefix: bytes,
    recorded_by: str,
    db: Any,
) -> dict[str, Any] | None:
    """Get the most recent path secret at a specific tree position.

    Args:
        depth: Tree depth
        path_prefix: Path prefix bytes
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Secret record dict or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Pad path_prefix to 16 bytes to match wire format storage
    path_prefix_padded = (path_prefix + b"\x00" * 16)[:16]

    return safedb.query_one(
        """SELECT treekem_secret_id, depth, path_prefix, key, recorded_at
           FROM treekem_secrets
           WHERE depth = ? AND path_prefix = ? AND recorded_by = ?
           ORDER BY recorded_at DESC LIMIT 1""",
        (depth, path_prefix_padded, recorded_by)
    )


def list_secrets(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all treekem_secrets for a peer.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of secret records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT treekem_secret_id, depth, path_prefix, key, recorded_at
           FROM treekem_secrets WHERE recorded_by = ?
           ORDER BY recorded_at DESC""",
        (peer_id,)
    ))


def get_root_secret(recorded_by: str, db: Any) -> bytes | None:
    """Get the root secret (depth=0, path_prefix=empty) for the current tree.

    The root secret is derived via KDF from path secrets. When all active peers
    have posted updates and shared their path secrets, everyone converges to
    the same root secret - enabling O(1) sender key broadcast.

    Args:
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Root secret key bytes (32 bytes) or None if not found
    """
    # Root is at depth=0 with empty path_prefix (padded to 16 zeros)
    root = get_secret_at_position(
        depth=0,
        path_prefix=b'',
        recorded_by=recorded_by,
        db=db,
    )
    return root['key'] if root else None


def get_root_secret_key_data(recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the root secret as a key_data dict for crypto operations.

    Args:
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Key dict {'id': bytes, 'key': bytes, 'type': 'symmetric'} or None
    """
    root = get_secret_at_position(
        depth=0,
        path_prefix=b'',
        recorded_by=recorded_by,
        db=db,
    )
    if not root:
        return None

    return {
        'id': crypto.b64decode(root['treekem_secret_id']),
        'key': root['key'],
        'type': 'symmetric',
    }
