"""TreeKEM Phase 2: treekem_pubkey event type (shareable public key at tree node).

This is a node in the TreeKEM hash-trie, storing a public key at a specific
tree position (depth, path_prefix). Signed, shareable events that enable
O(log n) key distribution by allowing peers to encrypt to tree nodes.
"""

# Registry metadata
EVENT_TYPE = 'treekem_pubkey'
SHAREABLE = True  # Syncs to peers
PROJECTION_TABLE = ('treekem_pubkeys', 'treekem_pubkey_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Event specification - signed, shareable
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
    """Pure projector for treekem_pubkey events.

    Pubkeys are signed events that advertise public keys at tree node positions
    for TreeKEM key distribution.
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'treekem_pubkey':
        return ProjectorResult(writes=tuple(), valid_event=False)

    public_key_b64 = event_data.get('public_key')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')
    depth = event_data.get('depth')
    path_prefix_b64 = event_data.get('path_prefix', '')
    parent_pubkey_id = event_data.get('parent_pubkey_id')
    removal_epoch_id = event_data.get('removal_epoch_id')

    if not public_key_b64 or not signed_by or created_at is None or depth is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Validate public key
    try:
        public_key = crypto.b64decode(public_key_b64)
        if len(public_key) != 32:
            return ProjectorResult(writes=tuple(), valid_event=False)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Decode path_prefix (may be empty string for root)
    path_prefix = crypto.b64decode(path_prefix_b64) if path_prefix_b64 else b''

    writes = (
        WriteOp(
            op='insert',
            table='treekem_pubkeys',
            values={
                'treekem_pubkey_id': ctx.event_id,
                'depth': depth,
                'path_prefix': path_prefix,
                'public_key': public_key,
                'owner_peer_id': signed_by,
                'parent_pubkey_id': parent_pubkey_id,
                'removal_epoch_id': removal_epoch_id,
                'created_at': created_at,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(
    depth: int,
    path_prefix: bytes,
    parent_pubkey_id: str | None,
    removal_epoch_id: str | None,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> tuple[str, bytes]:
    """Create a new treekem_pubkey event with a fresh keypair.

    Args:
        depth: Tree depth (0 = root)
        path_prefix: Path prefix bytes for this tree node
        parent_pubkey_id: Parent pubkey in the tree (None for root)
        removal_epoch_id: Current removal epoch (for forward secrecy)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (signer identity)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (treekem_pubkey_id, private_key): The pubkey event ID and private key bytes
    """
    log.info(f"treekem_pubkey.create() depth={depth}, peer_id={peer_id}, t_ms={t_ms}")

    # Generate Ed25519 keypair
    private_key, public_key = crypto.generate_keypair()

    # Get private key for signing
    from events.identity import peer
    signing_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wire event
    blob = wire_format.encode_treekem_pubkey_wire_event(
        depth=depth,
        path_prefix=path_prefix,
        public_key=public_key,
        parent_pubkey_id_b64=parent_pubkey_id,
        removal_epoch_id_b64=removal_epoch_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=signing_key,
    )

    # Store event
    treekem_pubkey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"treekem_pubkey.create() created treekem_pubkey_id={treekem_pubkey_id}")

    # Store the private key locally (not synced)
    safedb = create_safe_db(db, recorded_by=peer_id)
    safedb.execute(
        """INSERT INTO treekem_pubkey_secrets (treekem_pubkey_id, private_key, recorded_by)
           VALUES (?, ?, ?)""",
        (treekem_pubkey_id, private_key, peer_id)
    )

    return treekem_pubkey_id, private_key


def create_from_material(
    depth: int,
    path_prefix: bytes,
    public_key: bytes,
    private_key: bytes,
    parent_pubkey_id: str | None,
    removal_epoch_id: str | None,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create treekem_pubkey event from provided key material.

    Args:
        depth: Tree depth
        path_prefix: Path prefix bytes
        public_key: Ed25519 public key bytes
        private_key: Ed25519 private key bytes
        parent_pubkey_id: Parent pubkey ID
        removal_epoch_id: Removal epoch ID
        peer_id: Local peer ID
        peer_shared_id: Public peer ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        treekem_pubkey_id: The event ID
    """
    log.info(f"treekem_pubkey.create_from_material() depth={depth}, peer_id={peer_id}, t_ms={t_ms}")

    # Get private key for signing
    from events.identity import peer
    signing_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wire event
    blob = wire_format.encode_treekem_pubkey_wire_event(
        depth=depth,
        path_prefix=path_prefix,
        public_key=public_key,
        parent_pubkey_id_b64=parent_pubkey_id,
        removal_epoch_id_b64=removal_epoch_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=signing_key,
    )

    # Store event
    treekem_pubkey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"treekem_pubkey.create_from_material() created treekem_pubkey_id={treekem_pubkey_id}")

    # Store the private key locally (not synced)
    safedb = create_safe_db(db, recorded_by=peer_id)
    safedb.execute(
        """INSERT INTO treekem_pubkey_secrets (treekem_pubkey_id, private_key, recorded_by)
           VALUES (?, ?, ?)""",
        (treekem_pubkey_id, private_key, peer_id)
    )

    return treekem_pubkey_id


def get_pubkey_at_position(
    depth: int,
    path_prefix: bytes,
    recorded_by: str,
    db: Any,
    removal_epoch_id: str | None = None,
) -> dict[str, Any] | None:
    """Get the most recent pubkey at a specific tree position.

    Args:
        depth: Tree depth
        path_prefix: Path prefix bytes
        recorded_by: Local peer ID requesting
        db: Database connection
        removal_epoch_id: Optional removal epoch filter

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'type': 'asymmetric'}
        or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Pad path_prefix to 16 bytes to match wire format storage
    path_prefix_padded = (path_prefix + b"\x00" * 16)[:16]

    if removal_epoch_id is not None:
        result = safedb.query_one(
            """SELECT treekem_pubkey_id, public_key FROM treekem_pubkeys
               WHERE depth = ? AND path_prefix = ? AND removal_epoch_id = ? AND recorded_by = ?
               ORDER BY created_at DESC LIMIT 1""",
            (depth, path_prefix_padded, removal_epoch_id, recorded_by)
        )
    else:
        result = safedb.query_one(
            """SELECT treekem_pubkey_id, public_key FROM treekem_pubkeys
               WHERE depth = ? AND path_prefix = ? AND removal_epoch_id IS NULL AND recorded_by = ?
               ORDER BY created_at DESC LIMIT 1""",
            (depth, path_prefix_padded, recorded_by)
        )

    if not result:
        return None

    return {
        'id': crypto.b64decode(result['treekem_pubkey_id']),
        'public_key': result['public_key'],
        'type': 'asymmetric'
    }


def get_pubkey_by_id(
    treekem_pubkey_id: str,
    recorded_by: str,
    db: Any,
) -> dict[str, Any] | None:
    """Get a pubkey by its ID.

    Args:
        treekem_pubkey_id: The pubkey event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Pubkey record dict or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    result = safedb.query_one(
        """SELECT treekem_pubkey_id, depth, path_prefix, public_key, owner_peer_id,
                  parent_pubkey_id, removal_epoch_id, created_at
           FROM treekem_pubkeys
           WHERE treekem_pubkey_id = ? AND recorded_by = ?""",
        (treekem_pubkey_id, recorded_by)
    )
    return result


def get_private_key(treekem_pubkey_id: str, recorded_by: str, db: Any) -> bytes | None:
    """Get the private key for a treekem_pubkey we own.

    Args:
        treekem_pubkey_id: The pubkey event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Private key bytes or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT private_key FROM treekem_pubkey_secrets WHERE treekem_pubkey_id = ? AND recorded_by = ?",
        (treekem_pubkey_id, recorded_by)
    )
    return row['private_key'] if row else None


def get_pubkeys_by_owner(owner_peer_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """Get all pubkeys owned by a specific peer.

    Args:
        owner_peer_id: The owner's peer_shared_id
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of pubkey records
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return list(safedb.query(
        """SELECT treekem_pubkey_id, depth, path_prefix, public_key, parent_pubkey_id,
                  removal_epoch_id, created_at
           FROM treekem_pubkeys WHERE owner_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC""",
        (owner_peer_id, recorded_by)
    ))


def list_pubkeys(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all treekem_pubkeys visible to a peer.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of pubkey records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT treekem_pubkey_id, depth, path_prefix, public_key, owner_peer_id,
                  parent_pubkey_id, removal_epoch_id, created_at
           FROM treekem_pubkeys WHERE recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id,)
    ))
