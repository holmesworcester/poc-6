"""TreeKEM pubkey_shared event type (shareable tree node public key).

This is the SHAREABLE event that distributes tree node public keys to peers.
The keypair is stored in the local treekem_pubkey event. This pattern ensures
private keys survive replay.

Pattern: treekem_pubkey (local, SHAREABLE=False) + treekem_pubkey_shared (shareable, SHAREABLE=True)
Similar to: pubkey + pubkey_shared
"""

# Registry metadata
EVENT_TYPE = 'treekem_pubkey_shared'
SHAREABLE = True  # Tree node pubkeys sync for key distribution
PROJECTION_TABLE = ('treekem_pubkeys_shared', 'treekem_pubkey_shared_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from events.identity import peer
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# v2 event specification - signed by peer_shared
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {
        # Parent pubkey dependency - creates transitive dependency chain
        # from child → parent → grandparent → ... → root
        'parent_pubkey': {
            'source': 'table',
            'key_from': 'parent_pubkey_shared_id',
            'key': 'treekem_pubkey_shared_id',
            'table': 'treekem_pubkeys_shared',
            'required_if_present': True,  # Block until parent is valid
        },
    },
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for treekem_pubkey_shared events.

    treekem_pubkey_shared events share tree node public keys with peers for
    TreeKEM key distribution.
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'treekem_pubkey_shared':
        return ProjectorResult(writes=tuple(), valid_event=False)

    treekem_pubkey_id = event_data.get('treekem_pubkey_id')
    public_key_b64 = event_data.get('public_key')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')
    depth = event_data.get('depth')
    path_prefix_b64 = event_data.get('path_prefix', '')
    parent_pubkey_shared_id = event_data.get('parent_pubkey_shared_id')
    removal_epoch_id = event_data.get('removal_epoch_id')

    if not all([treekem_pubkey_id, public_key_b64, signed_by, created_at is not None, depth is not None]):
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
            table='treekem_pubkeys_shared',
            values={
                'treekem_pubkey_shared_id': ctx.event_id,
                'treekem_pubkey_id': treekem_pubkey_id,
                'depth': depth,
                'path_prefix': path_prefix,
                'public_key': public_key,
                'owner_peer_id': signed_by,
                'parent_pubkey_shared_id': parent_pubkey_shared_id,
                'removal_epoch_id': removal_epoch_id,
                'created_at': created_at,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(
    treekem_pubkey_id: str,
    depth: int,
    path_prefix: bytes,
    parent_pubkey_shared_id: str | None,
    removal_epoch_id: str | None,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create a shareable treekem_pubkey_shared event from a local treekem_pubkey.

    Args:
        treekem_pubkey_id: Local treekem_pubkey event ID (to get public key from)
        depth: Tree depth (0 = root)
        path_prefix: Path prefix bytes for this tree node
        parent_pubkey_shared_id: Parent pubkey in the tree (None for root)
        removal_epoch_id: Current removal epoch (for forward secrecy)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (signer identity)
        t_ms: Timestamp
        db: Database connection

    Returns:
        treekem_pubkey_shared_id: The stored event ID
    """
    log.info(f"treekem_pubkey_shared.create() creating for treekem_pubkey_id={treekem_pubkey_id[:20]}..., depth={depth}")

    # Get public key from local treekem_pubkey event
    from events.group import treekem_pubkey
    public_key = treekem_pubkey.get_public_key(treekem_pubkey_id, peer_id, db)
    if not public_key:
        raise ValueError(f"treekem_pubkey not found: {treekem_pubkey_id}")

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)

    blob = wire_format.encode_treekem_pubkey_shared_wire_event(
        treekem_pubkey_id_b64=treekem_pubkey_id,
        depth=depth,
        path_prefix=path_prefix,
        public_key=public_key,
        parent_pubkey_shared_id_b64=parent_pubkey_shared_id,
        removal_epoch_id_b64=removal_epoch_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )

    # Store event
    treekem_pubkey_shared_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"treekem_pubkey_shared.create() created treekem_pubkey_shared_id={treekem_pubkey_shared_id[:20]}...")

    return treekem_pubkey_shared_id


def get_pubkey_at_position(
    depth: int,
    path_prefix: bytes,
    recorded_by: str,
    db: Any,
    removal_epoch_id: str | None = None,
) -> dict[str, Any] | None:
    """Get the most recent VALID pubkey at a specific tree position.

    A pubkey is valid if:
    1. It exists at the (depth, path_prefix) position
    2. Its owner is NOT removed in the current removal_epoch

    This enables reusing old pubkeys for efficient removal handling:
    after a removal, we can still use pubkeys from non-removed members
    that were created in previous epochs.

    Args:
        depth: Tree depth
        path_prefix: Path prefix bytes
        recorded_by: Local peer ID requesting
        db: Database connection
        removal_epoch_id: Current removal epoch for filtering removed owners

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'owner_peer_id': str, 'type': 'asymmetric'}
        or None if not found
    """
    from events.identity import removal_epoch as removal_epoch_module

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Pad path_prefix to 16 bytes to match wire format storage
    path_prefix_padded = (path_prefix + b"\x00" * 16)[:16]

    # Get all pubkeys at this position (any epoch), most recent first
    # We search across ALL epochs, not just the current one, to enable
    # reusing old pubkeys from non-removed members
    results = safedb.query(
        """SELECT treekem_pubkey_id, public_key, owner_peer_id
           FROM treekem_pubkeys_shared
           WHERE depth = ? AND path_prefix = ? AND recorded_by = ?
           ORDER BY created_at DESC""",
        (depth, path_prefix_padded, recorded_by)
    )

    # Return first pubkey whose owner is NOT removed
    for result in results:
        owner = result['owner_peer_id']

        # Check if owner is removed in the current epoch
        if removal_epoch_id is not None:
            if removal_epoch_module.is_removed(owner, removal_epoch_id, recorded_by, db):
                continue  # Skip pubkeys from removed owners

        return {
            'id': crypto.b64decode(result['treekem_pubkey_id']),
            'public_key': result['public_key'],
            'owner_peer_id': owner,
            'type': 'asymmetric'
        }

    return None


def get_pubkey_by_id(
    treekem_pubkey_id: str,
    recorded_by: str,
    db: Any,
) -> dict[str, Any] | None:
    """Get a pubkey by its treekem_pubkey_id from the shared table.

    Args:
        treekem_pubkey_id: The local treekem_pubkey event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Pubkey record dict or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    result = safedb.query_one(
        """SELECT treekem_pubkey_shared_id, treekem_pubkey_id, depth, path_prefix, public_key,
                  owner_peer_id, parent_pubkey_shared_id, removal_epoch_id, created_at
           FROM treekem_pubkeys_shared
           WHERE treekem_pubkey_id = ? AND recorded_by = ?""",
        (treekem_pubkey_id, recorded_by)
    )
    return result


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
        """SELECT treekem_pubkey_shared_id, treekem_pubkey_id, depth, path_prefix, public_key,
                  parent_pubkey_shared_id, removal_epoch_id, created_at
           FROM treekem_pubkeys_shared WHERE owner_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC""",
        (owner_peer_id, recorded_by)
    ))


def list_pubkeys(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all treekem_pubkeys_shared visible to a peer.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of pubkey records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT treekem_pubkey_shared_id, treekem_pubkey_id, depth, path_prefix, public_key,
                  owner_peer_id, parent_pubkey_shared_id, removal_epoch_id, created_at
           FROM treekem_pubkeys_shared WHERE recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id,)
    ))


def get_resolution(
    depth: int,
    path_prefix: bytes,
    max_depth: int,
    recorded_by: str,
    db: Any,
    removal_epoch_id: str | None = None,
) -> list[dict[str, Any]]:
    """Find resolution of a potentially blank node for DH-based TreeKEM.

    In TreeKEM, when a sibling node is blank (no pubkey), we need to find
    its "resolution" - the highest non-blank descendants.

    Example tree:
    ```
           root (depth 0)
          /    \\
       d=1    d=1 [BLANK]
       / \\     / \\
      A   B   C   D   (leaves at depth 2)
    ```

    If A updates and sibling at (d=1, prefix=0x01) is BLANK:
    - Resolution = [C, D] (highest non-blank under that subtree)
    - A must do separate DH+encryption for C AND D

    Args:
        depth: Tree depth of the node to resolve
        path_prefix: Path prefix bytes for this node
        max_depth: Maximum tree depth (leaf level)
        recorded_by: Local peer ID
        db: Database connection
        removal_epoch_id: Current removal epoch for filtering removed owners

    Returns:
        List of pubkey dicts for DH, each with 'id', 'public_key', 'owner_peer_id', 'type'
        If node has pubkey, returns just that pubkey.
        If blank, recursively finds highest non-blank descendants.
    """
    # Check if this node has a pubkey
    pubkey = get_pubkey_at_position(depth, path_prefix, recorded_by, db, removal_epoch_id)
    if pubkey:
        return [pubkey]

    # Node is blank - recurse to children
    if depth >= max_depth:
        return []  # Leaf is blank (member removed or never joined)

    # Build child prefixes by extending with 0 or 1
    # Child prefix = parent prefix with one more bit
    # Since path_prefix is byte-aligned, we need to handle bit positions
    child_depth = depth + 1

    # For the left child (bit = 0), keep same prefix bytes
    # For the right child (bit = 1), set the bit at position 'depth'
    left_prefix = path_prefix

    # Calculate which byte and bit to set for right child
    # Bit position 'depth' means we're setting bit (7 - depth % 8) in byte (depth // 8)
    byte_idx = depth // 8
    bit_idx = 7 - (depth % 8)

    # Extend path_prefix if needed
    right_prefix_list = list(path_prefix) + [0] * (byte_idx + 1 - len(path_prefix))
    right_prefix_list[byte_idx] |= (1 << bit_idx)
    right_prefix = bytes(right_prefix_list)

    left_resolution = get_resolution(
        child_depth, left_prefix, max_depth, recorded_by, db, removal_epoch_id
    )
    right_resolution = get_resolution(
        child_depth, right_prefix, max_depth, recorded_by, db, removal_epoch_id
    )

    return left_resolution + right_resolution


def get_sibling_position(peer_shared_id: str, depth: int) -> tuple[int, bytes]:
    """Get the sibling's tree position at a given depth.

    Args:
        peer_shared_id: The peer's shared ID (determines their path)
        depth: The depth to get sibling at

    Returns:
        (sibling_depth, sibling_prefix) tuple
    """
    from core import treekem

    # Get our path prefix at this depth
    my_prefix = treekem.get_path_prefix(peer_shared_id, depth)

    # Sibling has the same prefix but with the last bit flipped
    sibling_prefix = treekem.get_copath_prefix(peer_shared_id, depth)

    return (depth, sibling_prefix)
