"""TreeKEM Phase 2: treekem_secret event type (local-only symmetric key at tree node).

This is a node in the TreeKEM hash-trie, storing a symmetric key at a specific
tree position (depth, path_prefix). Local-only, deterministic events that
enable O(log n) key distribution.
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

    Secrets are local-only deterministic events (no timestamp in blob).
    Uses recorded_at as created_at.
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'treekem_secret':
        return ProjectorResult(writes=tuple(), valid_event=False)

    key_b64 = event_data.get('key')
    depth = event_data.get('depth')
    path_prefix_b64 = event_data.get('path_prefix', '')

    if not key_b64 or depth is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    try:
        key_bytes = crypto.b64decode(key_b64)
        if len(key_bytes) != 32:
            return ProjectorResult(writes=tuple(), valid_event=False)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Decode path_prefix (may be empty string for root)
    path_prefix = crypto.b64decode(path_prefix_b64) if path_prefix_b64 else b''

    writes = (
        WriteOp(
            op='insert',
            table='treekem_secrets',
            values={
                'treekem_secret_id': ctx.event_id,
                'depth': depth,
                'path_prefix': path_prefix,
                'key': key_bytes,
                'created_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(
    depth: int,
    path_prefix: bytes,
    peer_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create a new treekem_secret with a fresh symmetric key.

    Args:
        depth: Tree depth (0 = root)
        path_prefix: Path prefix bytes for this tree node
        peer_id: Local peer ID (owner)
        t_ms: Timestamp
        db: Database connection

    Returns:
        treekem_secret_id: The event ID
    """
    log.info(f"treekem_secret.create() depth={depth}, prefix_len={len(path_prefix)}, peer_id={peer_id}")

    # Generate symmetric key
    key = crypto.generate_secret()

    return create_with_material(
        depth=depth,
        path_prefix=path_prefix,
        key_material=key,
        peer_id=peer_id,
        t_ms=t_ms,
        db=db,
    )


def create_with_material(
    depth: int,
    path_prefix: bytes,
    key_material: bytes,
    peer_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create treekem_secret event with provided key material.

    Creates a DETERMINISTIC secret event from the key material.
    Same (depth, path_prefix, key_material) = same treekem_secret_id on all peers.

    Args:
        depth: Tree depth
        path_prefix: Path prefix bytes
        key_material: The symmetric key bytes (32 bytes)
        peer_id: Peer ID that owns this key
        t_ms: Timestamp (used for recorded_at, NOT in the blob)
        db: Database connection

    Returns:
        treekem_secret_id: The deterministic event ID
    """
    log.info(f"treekem_secret.create_with_material() depth={depth}, peer_id={peer_id}")

    if len(key_material) != 32:
        raise ValueError(f"key_material must be 32 bytes, got {len(key_material)}")

    # Create DETERMINISTIC event blob
    blob = wire_format.encode_treekem_secret_wire_event(
        depth=depth,
        path_prefix=path_prefix,
        key=key_material,
        created_at_ms=0,  # Deterministic
    )

    treekem_secret_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"treekem_secret.create_with_material() created treekem_secret_id={treekem_secret_id}")
    return treekem_secret_id


def get_key(treekem_secret_id: str, recorded_by: str, db: Any) -> dict[str, Any]:
    """Get treekem_secret from database in format expected by crypto operations.

    Args:
        treekem_secret_id: The treekem_secret event ID
        recorded_by: Peer ID requesting access
        db: Database connection

    Returns:
        Key dict for crypto operations

    Raises:
        ValueError: If secret not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT key, depth, path_prefix FROM treekem_secrets WHERE treekem_secret_id = ? AND recorded_by = ?",
        (treekem_secret_id, recorded_by)
    )
    if not row:
        raise ValueError(f"treekem_secret not found: {treekem_secret_id}")

    return {
        'id': crypto.b64decode(treekem_secret_id),
        'key': row['key'],
        'type': 'symmetric',
        'depth': row['depth'],
        'path_prefix': row['path_prefix'],
    }


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


def get_by_position(depth: int, path_prefix: bytes, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the most recent treekem_secret at a specific tree position.

    Args:
        depth: Tree depth
        path_prefix: Path prefix bytes
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Secret record dict or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        """SELECT treekem_secret_id, depth, path_prefix, key, created_at
           FROM treekem_secrets
           WHERE depth = ? AND path_prefix = ? AND recorded_by = ?
           ORDER BY created_at DESC LIMIT 1""",
        (depth, path_prefix, recorded_by)
    )
    return row


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
        """SELECT treekem_secret_id, depth, path_prefix, created_at
           FROM treekem_secrets WHERE recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id,)
    ))
