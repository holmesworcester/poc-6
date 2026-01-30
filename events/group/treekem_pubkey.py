"""TreeKEM pubkey event type (local-only keypair storage for tree nodes).

This is the LOCAL event that stores the keypair for a tree node. The public key
and tree position are shared via treekem_pubkey_shared events. This pattern ensures
private keys survive replay since local events are not affected by project_pure()
during replay.

Pattern: treekem_pubkey (local, SHAREABLE=False) + treekem_pubkey_shared (shareable, SHAREABLE=True)
Similar to: pubkey + pubkey_shared
"""

# Registry metadata
EVENT_TYPE = 'treekem_pubkey'
SHAREABLE = False  # Local-only - contains private key material
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# v2 event specification - no signer, no deps (local-only deterministic event)
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for treekem_pubkey events.

    treekem_pubkey events are local-only deterministic events (created_at=0 in blob).
    Uses recorded_at as created_at and recorded_by as owner_peer_id.
    Stores both public and private key in the treekem_pubkeys table.
    """
    event_data = ctx.event_data

    public_key_b64 = event_data.get('public_key')
    private_key_b64 = event_data.get('private_key')

    if not public_key_b64 or not private_key_b64:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Validate and decode keys
    try:
        public_key = crypto.b64decode(public_key_b64)
        private_key = crypto.b64decode(private_key_b64)
        if len(public_key) != 32 or len(private_key) != 32:
            return ProjectorResult(writes=tuple(), valid_event=False)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Use recorded_at as created_at (deterministic blobs have no timestamp)
    created_at = ctx.recorded_at

    writes = (
        WriteOp(
            op='insert',
            table='treekem_pubkeys',
            values={
                'treekem_pubkey_id': ctx.event_id,
                'owner_peer_id': ctx.recorded_by,
                'public_key': public_key,
                'private_key': private_key,
                'created_at': created_at,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(
    depth: int,
    path_prefix: bytes,
    parent_pubkey_shared_id: str | None,
    removal_epoch_id: str | None,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> tuple[str, str, bytes]:
    """Create a new treekem_pubkey at a tree position (creates both local and shared events).

    This is the main entry point for creating tree node keys. It:
    1. Creates the LOCAL event that stores the keypair
    2. Creates the SHARED event that advertises the tree position + public key

    Args:
        depth: Tree depth (0 = root)
        path_prefix: Path prefix bytes for this tree node
        parent_pubkey_shared_id: Parent's treekem_pubkey_shared_id (None for root)
            This is the SHAREABLE event ID for dependency ordering.
        removal_epoch_id: Current removal epoch (for forward secrecy)
        peer_id: Local peer ID (owner of this keypair)
        peer_shared_id: Public peer ID (signer identity)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (treekem_pubkey_id, treekem_pubkey_shared_id, private_key):
            - treekem_pubkey_id: Local event ID (for private key lookup)
            - treekem_pubkey_shared_id: Shareable event ID (for dependency references)
            - private_key: The Ed25519 private key bytes
    """
    log.info(f"treekem_pubkey.create() creating treekem_pubkey at depth={depth} for peer_id={peer_id}, t_ms={t_ms}")

    # Step 1: Create the local keypair event
    treekem_pubkey_id, private_key = _create_local(peer_id, t_ms, db)

    # Step 2: Create the shared event that advertises the tree position
    from events.group import treekem_pubkey_shared
    treekem_pubkey_shared_id = treekem_pubkey_shared.create(
        treekem_pubkey_id=treekem_pubkey_id,
        depth=depth,
        path_prefix=path_prefix,
        parent_pubkey_shared_id=parent_pubkey_shared_id,
        removal_epoch_id=removal_epoch_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,
        db=db,
    )

    return treekem_pubkey_id, treekem_pubkey_shared_id, private_key


def _create_local(peer_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a new local treekem_pubkey event with a fresh keypair (internal).

    This creates the LOCAL event that stores the keypair. Called by create().

    Args:
        peer_id: Local peer ID (owner of this keypair)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (treekem_pubkey_id, private_key): The treekem_pubkey event ID and private key bytes
    """
    log.info(f"treekem_pubkey._create_local() creating keypair for peer_id={peer_id}, t_ms={t_ms}")

    # Generate Ed25519 keypair
    private_key, public_key = crypto.generate_keypair()

    # Create DETERMINISTIC event blob - only type and keys, created_at=0
    # This ensures same key material = same treekem_pubkey_id on all peers
    blob = wire_format.encode_treekem_pubkey_local_event(
        public_key=public_key,
        private_key=private_key,
    )

    # Store event
    treekem_pubkey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"treekem_pubkey.create() created treekem_pubkey_id={treekem_pubkey_id}")

    return treekem_pubkey_id, private_key


def create_from_material(public_key: bytes, private_key: bytes, peer_id: str,
                         t_ms: int, db: Any) -> str:
    """Create treekem_pubkey event from provided key material.

    Creates a DETERMINISTIC treekem_pubkey event from the key material.
    Same key material = same treekem_pubkey_id on all peers.

    Args:
        public_key: Ed25519 public key bytes
        private_key: Ed25519 private key bytes
        peer_id: Peer ID that owns this keypair
        t_ms: Timestamp (used for recorded_at, NOT in the blob)
        db: Database connection

    Returns:
        treekem_pubkey_id: The deterministic event ID
    """
    log.info(f"treekem_pubkey.create_from_material() creating treekem_pubkey for peer_id={peer_id}, t_ms={t_ms}")

    # Create DETERMINISTIC event blob
    blob = wire_format.encode_treekem_pubkey_local_event(
        public_key=public_key,
        private_key=private_key,
    )

    # Store event
    treekem_pubkey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"treekem_pubkey.create_from_material() created treekem_pubkey_id={treekem_pubkey_id}")

    return treekem_pubkey_id


def get_private_key(treekem_pubkey_id: str, recorded_by: str, db: Any) -> bytes | None:
    """Get the private key for a treekem_pubkey we own.

    Args:
        treekem_pubkey_id: The treekem_pubkey event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Private key bytes or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT private_key FROM treekem_pubkeys WHERE treekem_pubkey_id = ? AND recorded_by = ?",
        (treekem_pubkey_id, recorded_by)
    )
    return row['private_key'] if row else None


def get_public_key(treekem_pubkey_id: str, recorded_by: str, db: Any) -> bytes | None:
    """Get the public key for a treekem_pubkey.

    Args:
        treekem_pubkey_id: The treekem_pubkey event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Public key bytes or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT public_key FROM treekem_pubkeys WHERE treekem_pubkey_id = ? AND recorded_by = ?",
        (treekem_pubkey_id, recorded_by)
    )
    return row['public_key'] if row else None


def get_treekem_pubkey_by_id(treekem_pubkey_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get treekem_pubkey data by ID.

    This looks up a treekem_pubkey by its ID. It first checks the local treekem_pubkeys table
    (for keys we own with private key), then falls back to treekem_pubkeys_shared
    (for other peers' public keys).

    Args:
        treekem_pubkey_id: The treekem_pubkey ID to look up
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'type': 'asymmetric', 'owner_peer_id': str}
        or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # First try local treekem_pubkeys (we own these and have private key)
    local_row = safedb.query_one(
        "SELECT treekem_pubkey_id, public_key, owner_peer_id FROM treekem_pubkeys WHERE treekem_pubkey_id = ? AND recorded_by = ?",
        (treekem_pubkey_id, recorded_by)
    )

    # Also check shared table for tree position info (depth, path_prefix, etc.)
    shared_row = safedb.query_one(
        """SELECT depth, path_prefix, parent_pubkey_shared_id, removal_epoch_id
           FROM treekem_pubkeys_shared WHERE treekem_pubkey_id = ? AND recorded_by = ?""",
        (treekem_pubkey_id, recorded_by)
    )

    if local_row:
        result = {
            'id': crypto.b64decode(local_row['treekem_pubkey_id']),
            'public_key': local_row['public_key'],
            'owner_peer_id': local_row['owner_peer_id'],
            'type': 'asymmetric',
        }
        # Add tree position from shared table if available
        if shared_row:
            result['depth'] = shared_row['depth']
            result['path_prefix'] = shared_row['path_prefix']
            result['parent_pubkey_shared_id'] = shared_row['parent_pubkey_shared_id']
            result['removal_epoch_id'] = shared_row['removal_epoch_id']
        return result

    # Fall back to treekem_pubkeys_shared (other peers' public keys)
    row = safedb.query_one(
        """SELECT treekem_pubkey_id, public_key, owner_peer_id, depth, path_prefix,
                  parent_pubkey_shared_id, removal_epoch_id
           FROM treekem_pubkeys_shared WHERE treekem_pubkey_id = ? AND recorded_by = ?""",
        (treekem_pubkey_id, recorded_by)
    )
    if row:
        return {
            'id': crypto.b64decode(row['treekem_pubkey_id']),
            'public_key': row['public_key'],
            'owner_peer_id': row['owner_peer_id'],
            'depth': row['depth'],
            'path_prefix': row['path_prefix'],
            'parent_pubkey_shared_id': row['parent_pubkey_shared_id'],
            'removal_epoch_id': row['removal_epoch_id'],
            'type': 'asymmetric',
        }

    return None


def list_own_treekem_pubkeys(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all treekem_pubkeys owned by this peer.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of treekem_pubkey records with treekem_pubkey_id, public_key, created_at
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT treekem_pubkey_id, public_key, created_at
           FROM treekem_pubkeys WHERE owner_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id, peer_id)
    ))


# Convenience functions that delegate to treekem_pubkey_shared module
# for backward compatibility with code that expects these on treekem_pubkey

def get_pubkey_at_position(
    depth: int,
    path_prefix: bytes,
    recorded_by: str,
    db: Any,
    removal_epoch_id: str | None = None,
) -> dict[str, Any] | None:
    """Get the most recent pubkey at a specific tree position.

    Delegates to treekem_pubkey_shared.get_pubkey_at_position().
    """
    from events.group import treekem_pubkey_shared
    return treekem_pubkey_shared.get_pubkey_at_position(
        depth=depth,
        path_prefix=path_prefix,
        recorded_by=recorded_by,
        db=db,
        removal_epoch_id=removal_epoch_id,
    )


def get_pubkeys_by_owner(owner_peer_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """Get all pubkeys owned by a specific peer.

    Delegates to treekem_pubkey_shared.get_pubkeys_by_owner().
    """
    from events.group import treekem_pubkey_shared
    return treekem_pubkey_shared.get_pubkeys_by_owner(
        owner_peer_id=owner_peer_id,
        recorded_by=recorded_by,
        db=db,
    )


def list_pubkeys(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all treekem_pubkeys visible to a peer.

    Delegates to treekem_pubkey_shared.list_pubkeys().
    """
    from events.group import treekem_pubkey_shared
    return treekem_pubkey_shared.list_pubkeys(peer_id=peer_id, db=db)


def get_pubkey_by_id(
    treekem_pubkey_id: str,
    recorded_by: str,
    db: Any,
) -> dict[str, Any] | None:
    """Get a pubkey by its ID.

    This looks up in both local (treekem_pubkeys) and shared (treekem_pubkeys_shared) tables.
    """
    return get_treekem_pubkey_by_id(treekem_pubkey_id, recorded_by, db)
