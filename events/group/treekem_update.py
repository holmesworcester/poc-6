"""TreeKEM Phase 2: treekem_update event type (update orchestration event).

This is the orchestration event that ties together an update path:
- References the author's leaf pubkey (for atomicity via transitive deps)
- Links to parent update for ordering
- Provides removal_epoch_id for forward secrecy

For concurrent updates, the update with the lowest treekem_update_id wins,
providing deterministic convergence across all peers.

ATOMICITY: The update depends on leaf_pubkey_shared_id, which transitively
depends on all parent pubkeys up to the root. This ensures the entire
update path is present before the update becomes valid.
"""

# Registry metadata
EVENT_TYPE = 'treekem_update'
SHAREABLE = True  # Syncs to peers
PROJECTION_TABLE = ('treekem_updates', 'treekem_update_id')

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
    'optional': {
        # Leaf pubkey dependency - creates transitive dependency chain
        # update → leaf → ... → root (ensures entire path exists)
        'leaf_pubkey': {
            'source': 'table',
            'key_from': 'leaf_pubkey_shared_id',
            'key': 'treekem_pubkey_shared_id',
            'table': 'treekem_pubkeys_shared',
            'required_if_present': True,  # Block until leaf (and ancestors) valid
        },
    },
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for treekem_update events.

    Updates are signed events that orchestrate TreeKEM key updates.
    The winning state is determined by lowest update_id among concurrent updates.
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'treekem_update':
        return ProjectorResult(writes=tuple(), valid_event=False)

    author_peer_id = event_data.get('author_peer_id')
    leaf_pubkey_shared_id = event_data.get('leaf_pubkey_shared_id')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')
    removal_epoch_id = event_data.get('removal_epoch_id')
    base_update_id = event_data.get('base_update_id')

    if not author_peer_id or not leaf_pubkey_shared_id or not signed_by or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Insert the update record
    writes = (
        WriteOp(
            op='insert',
            table='treekem_updates',
            values={
                'treekem_update_id': ctx.event_id,
                'author_peer_id': author_peer_id,
                'leaf_pubkey_shared_id': leaf_pubkey_shared_id,
                'removal_epoch_id': removal_epoch_id,
                'base_update_id': base_update_id,
                'created_at': created_at,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    # Winning state is computed at query time by get_winning_update()
    # rather than being maintained during projection, to avoid
    # complex upsert logic. The query sorts by update_id ASC and
    # takes the first result.

    return ProjectorResult(writes=writes, valid_event=True)


def create(
    leaf_pubkey_shared_id: str,
    removal_epoch_id: str | None,
    base_update_id: str | None,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create a new treekem_update event.

    Args:
        leaf_pubkey_shared_id: The leaf pubkey_shared ID for this update path.
            This is the SHAREABLE event ID at the bottom of the path.
            The update depends on this, which transitively depends on all
            ancestors up to the root (ensuring atomicity).
        removal_epoch_id: Current removal epoch (for forward secrecy)
        base_update_id: Previous update this builds on (None for first)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (signer identity)
        t_ms: Timestamp
        db: Database connection

    Returns:
        treekem_update_id: The update event ID
    """
    log.info(f"treekem_update.create() peer_id={peer_id}, leaf_pubkey_shared_id={leaf_pubkey_shared_id[:20]}...")

    # Get private key for signing
    from events.identity import peer
    signing_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wire event
    blob = wire_format.encode_treekem_update_wire_event(
        author_peer_id_b64=peer_shared_id,
        removal_epoch_id_b64=removal_epoch_id,
        base_update_id_b64=base_update_id,
        leaf_pubkey_shared_id_b64=leaf_pubkey_shared_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=signing_key,
    )

    # Store event
    treekem_update_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"treekem_update.create() created treekem_update_id={treekem_update_id}")

    return treekem_update_id


def get_update(treekem_update_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get an update by its ID.

    Args:
        treekem_update_id: The update event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Update record dict or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_one(
        """SELECT treekem_update_id, author_peer_id, leaf_pubkey_shared_id,
                  removal_epoch_id, base_update_id, created_at
           FROM treekem_updates
           WHERE treekem_update_id = ? AND recorded_by = ?""",
        (treekem_update_id, recorded_by)
    )


def get_winning_update(
    removal_epoch_id: str | None,
    base_update_id: str | None,
    recorded_by: str,
    db: Any,
) -> dict[str, Any] | None:
    """Get the winning update for a given (removal_epoch_id, base_update_id) pair.

    For concurrent updates, the update with the lowest ID wins.
    Winner is computed at query time by sorting updates by ID.

    Args:
        removal_epoch_id: Removal epoch ID (or None)
        base_update_id: Base update ID (or None)
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Winning update record or None
    """
    # Get concurrent updates sorted by ID (lowest first = winner)
    concurrent = get_concurrent_updates(base_update_id, removal_epoch_id, recorded_by, db)

    if not concurrent:
        return None

    # First one is the winner (lowest ID)
    return concurrent[0]


def get_latest_update(
    removal_epoch_id: str | None,
    recorded_by: str,
    db: Any,
) -> dict[str, Any] | None:
    """Get the most recent update for a removal epoch.

    Args:
        removal_epoch_id: Removal epoch ID (or None for initial)
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Latest update record or None
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    if removal_epoch_id is not None:
        return safedb.query_one(
            """SELECT treekem_update_id, author_peer_id, leaf_pubkey_shared_id,
                      removal_epoch_id, base_update_id, created_at
               FROM treekem_updates
               WHERE removal_epoch_id = ? AND recorded_by = ?
               ORDER BY created_at DESC LIMIT 1""",
            (removal_epoch_id, recorded_by)
        )
    else:
        return safedb.query_one(
            """SELECT treekem_update_id, author_peer_id, leaf_pubkey_shared_id,
                      removal_epoch_id, base_update_id, created_at
               FROM treekem_updates
               WHERE removal_epoch_id IS NULL AND recorded_by = ?
               ORDER BY created_at DESC LIMIT 1""",
            (recorded_by,)
        )


def get_concurrent_updates(
    base_update_id: str | None,
    removal_epoch_id: str | None,
    recorded_by: str,
    db: Any,
) -> list[dict[str, Any]]:
    """Get all updates that share the same base_update_id (concurrent updates).

    Args:
        base_update_id: The base update ID
        removal_epoch_id: Removal epoch ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of concurrent update records, sorted by update_id (winner first)
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    if base_update_id is None and removal_epoch_id is None:
        updates = safedb.query(
            """SELECT treekem_update_id, author_peer_id, leaf_pubkey_shared_id,
                      removal_epoch_id, base_update_id, created_at
               FROM treekem_updates
               WHERE base_update_id IS NULL AND removal_epoch_id IS NULL AND recorded_by = ?
               ORDER BY treekem_update_id ASC""",
            (recorded_by,)
        )
    elif base_update_id is None:
        updates = safedb.query(
            """SELECT treekem_update_id, author_peer_id, leaf_pubkey_shared_id,
                      removal_epoch_id, base_update_id, created_at
               FROM treekem_updates
               WHERE base_update_id IS NULL AND removal_epoch_id = ? AND recorded_by = ?
               ORDER BY treekem_update_id ASC""",
            (removal_epoch_id, recorded_by)
        )
    elif removal_epoch_id is None:
        updates = safedb.query(
            """SELECT treekem_update_id, author_peer_id, leaf_pubkey_shared_id,
                      removal_epoch_id, base_update_id, created_at
               FROM treekem_updates
               WHERE base_update_id = ? AND removal_epoch_id IS NULL AND recorded_by = ?
               ORDER BY treekem_update_id ASC""",
            (base_update_id, recorded_by)
        )
    else:
        updates = safedb.query(
            """SELECT treekem_update_id, author_peer_id, leaf_pubkey_shared_id,
                      removal_epoch_id, base_update_id, created_at
               FROM treekem_updates
               WHERE base_update_id = ? AND removal_epoch_id = ? AND recorded_by = ?
               ORDER BY treekem_update_id ASC""",
            (base_update_id, removal_epoch_id, recorded_by)
        )

    return list(updates)


def list_updates(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all treekem_updates visible to a peer.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of update records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT treekem_update_id, author_peer_id, leaf_pubkey_shared_id,
                  removal_epoch_id, base_update_id, created_at
           FROM treekem_updates WHERE recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id,)
    ))


def is_winning_update(treekem_update_id: str, recorded_by: str, db: Any) -> bool:
    """Check if an update is the winning update for its base.

    Args:
        treekem_update_id: The update to check
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        True if this is the winning update
    """
    update = get_update(treekem_update_id, recorded_by, db)
    if not update:
        return False

    winning = get_winning_update(
        update['removal_epoch_id'],
        update['base_update_id'],
        recorded_by,
        db
    )

    return winning is not None and winning['treekem_update_id'] == treekem_update_id


def get_winning_chain(
    removal_epoch_id: str | None,
    recorded_by: str,
    db: Any,
) -> list[dict[str, Any]]:
    """Get the full winning chain of updates for a removal epoch.

    Walks from the tip (latest winning update) back through base_update_id
    to build the complete chain of winning updates.

    Args:
        removal_epoch_id: Current removal epoch (or None)
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of updates in the winning chain, from tip to root (oldest first reversed)
    """
    chain = []

    # Start with no base (initial updates)
    base_update_id = None

    while True:
        # Get winning update at this level
        winning = get_winning_update(removal_epoch_id, base_update_id, recorded_by, db)
        if not winning:
            break

        chain.append(winning)
        base_update_id = winning['treekem_update_id']

        # Check if there are more updates building on this one
        next_updates = get_concurrent_updates(base_update_id, removal_epoch_id, recorded_by, db)
        if not next_updates:
            break

    return chain


def get_represented_peers(
    removal_epoch_id: str | None,
    recorded_by: str,
    db: Any,
) -> set[str]:
    """Get all peers represented in the winning tree state.

    A peer is "represented" if they authored an update in the winning chain.
    These peers are covered by the tree; others need leaf fallback.

    Args:
        removal_epoch_id: Current removal epoch (or None)
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Set of peer_shared_ids represented in the winning tree
    """
    chain = get_winning_chain(removal_epoch_id, recorded_by, db)
    return {update['author_peer_id'] for update in chain}


def get_tree_cover_for_sender(
    sender_peer_id: str,
    target_peers: set[str],
    recorded_by: str,
    db: Any,
    removal_epoch_id: str | None = None,
) -> list[str]:
    """Get treekem_pubkey IDs covering target peers.

    Looks for pubkeys at copath positions from ANY epoch,
    filtering out pubkeys whose owners are removed.

    Uses the copath algorithm: for each depth, if the sibling subtree
    contains any target peers, include that copath node's pubkey.

    Args:
        sender_peer_id: The sending peer's peer_shared_id
        target_peers: Set of peer_shared_ids to cover (active members)
        recorded_by: Local peer ID
        db: Database connection
        removal_epoch_id: Current removal epoch for filtering removed owners

    Returns:
        List of treekem_pubkey IDs for the tree cover
    """
    from core import treekem
    from events.group import treekem_pubkey_shared

    # Filter to peers other than sender
    other_peers = [p for p in target_peers if p != sender_peer_id]
    if not other_peers:
        return []

    # Compute copath for sender over target peers
    copath = treekem.compute_copath_for_sender(sender_peer_id, list(target_peers))

    # Get pubkeys at copath positions, passing removal_epoch_id to filter removed owners
    pubkey_ids = []
    for depth, path_prefix in copath:
        pk = treekem_pubkey_shared.get_pubkey_at_position(
            depth, path_prefix, recorded_by, db,
            removal_epoch_id=removal_epoch_id
        )
        if pk:
            pubkey_ids.append(crypto.b64encode(pk['id']))

    return pubkey_ids


def get_root_pubkey_shared_id(treekem_update_id: str, recorded_by: str, db: Any) -> str | None:
    """Get the root pubkey_shared_id for an update by walking up from the leaf.

    Args:
        treekem_update_id: The update event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        The root treekem_pubkey_shared_id or None if not found
    """
    update = get_update(treekem_update_id, recorded_by, db)
    if not update:
        return None

    leaf_id = update['leaf_pubkey_shared_id']
    if not leaf_id:
        return None

    # Walk up the parent chain to find the root
    from events.group import treekem_pubkey_shared
    current_id = leaf_id
    while True:
        pubkey = treekem_pubkey_shared.get_pubkey_by_id(current_id, recorded_by, db)
        if not pubkey:
            return None
        parent_id = pubkey.get('parent_pubkey_shared_id')
        if not parent_id:
            # No parent = this is the root
            return current_id
        current_id = parent_id


def create_update_path(
    peer_id: str,
    peer_shared_id: str,
    removal_epoch_id: str | None,
    base_update_id: str | None,
    copath_pubkey_ids: list[str],
    t_ms: int,
    db: Any,
) -> dict[str, Any]:
    """Create a complete TreeKEM update path with derived secrets.

    This is the main entry point for creating TreeKEM updates. It:
    1. Computes leaf position from peer_shared_id bits
    2. Creates a random secret at LEAF only
    3. Derives all parent secrets using KDF chain (leaf → root)
    4. For each depth, shares path secret to copath node pubkey
    5. Creates treekem_update event referencing leaf

    The KDF derivation ensures all recipients can derive the same root secret:
    - Alice creates leaf secret K_leaf (random)
    - Alice derives: K2 = K_leaf, K1 = KDF(K2), K0 = KDF(K1)
    - Bob receives K1 via treekem_secret_shared
    - Bob derives: K0 = KDF(K1) - same as Alice's K0!

    The copath_pubkey_ids should be pre-computed by the caller based on their
    view of which peers are in the group and which copath positions have pubkeys.

    Args:
        peer_id: Local peer ID (owner)
        peer_shared_id: Public peer ID (signer identity)
        removal_epoch_id: Current removal epoch (for forward secrecy) or None
        base_update_id: Previous update this builds on (None for first)
        copath_pubkey_ids: List of copath node treekem_pubkey IDs to share secrets to.
                          Order: depth=1 first, increasing depth. Empty list means
                          no copath sharing (e.g., single member group).
        t_ms: Timestamp
        db: Database connection

    Returns:
        Dict with:
        - treekem_update_id: The update event ID
        - path_secrets: List of (treekem_secret_id, depth, path_prefix) tuples
        - path_pubkeys: List of (treekem_pubkey_id, treekem_pubkey_shared_id, depth) tuples
        - shared_ids: List of treekem_secret_shared event IDs
    """
    from core import treekem
    from events.group import treekem_pubkey, treekem_secret, treekem_secret_shared

    log.info(f"treekem_update.create_update_path() peer={peer_shared_id[:20]}..., "
             f"copath_count={len(copath_pubkey_ids)}")

    # Determine path depth from copath pubkey count
    # With n copath nodes, we create secrets at depths 1..n+1 (plus root at 0)
    path_depth = len(copath_pubkey_ids) + 1 if copath_pubkey_ids else 1

    # Step 1: Create path secrets with KDF derivation from leaf to root
    # Key insight: Create random secret at LEAF, derive parents via KDF
    path_secrets: list[tuple[str, int, bytes]] = []

    # Generate random secret at leaf depth
    leaf_secret = crypto.generate_secret()
    current_secret = leaf_secret

    for depth in range(path_depth, -1, -1):  # From leaf depth down to root (0)
        path_prefix = treekem.get_path_prefix(peer_shared_id, depth)

        # Create secret with the current key material (random at leaf, derived at parents)
        secret_id = treekem_secret.create_with_material(
            depth=depth,
            path_prefix=path_prefix,
            key_material=current_secret,
            peer_id=peer_id,
            t_ms=t_ms,
            db=db,
        )
        path_secrets.append((secret_id, depth, path_prefix))
        t_ms += 1

        # Derive parent secret for next iteration (if not already at root)
        if depth > 0:
            current_secret = crypto.derive_path_secret(current_secret, depth, path_prefix)

    # Reverse so depth=0 is first (root to leaf order)
    path_secrets.reverse()

    # Step 2: Create path pubkeys from root to leaf
    path_pubkeys: list[tuple[str, str, int]] = []
    parent_pubkey_shared_id: str | None = None

    for secret_id, depth, path_prefix in path_secrets:
        # Create pubkey at this depth
        pubkey_id, pubkey_shared_id, _ = treekem_pubkey.create(
            depth=depth,
            path_prefix=path_prefix,
            parent_pubkey_shared_id=parent_pubkey_shared_id,
            removal_epoch_id=removal_epoch_id,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,
            db=db,
        )
        path_pubkeys.append((pubkey_id, pubkey_shared_id, depth))
        parent_pubkey_shared_id = pubkey_shared_id
        t_ms += 1

    # Step 3: Share path secrets to copath nodes
    # For each copath pubkey, share the corresponding path secret
    # copath_pubkey_ids[i] gets secret at depth i+1
    shared_ids: list[str] = []

    if copath_pubkey_ids:
        # Get secrets to share (skip root at depth=0)
        secrets_to_share = [(sid, d, p) for sid, d, p in path_secrets if d > 0]

        # Match secrets to copath pubkeys by depth
        # copath_pubkey_ids[0] -> depth 1, copath_pubkey_ids[1] -> depth 2, etc.
        for copath_pubkey_id in copath_pubkey_ids:
            # Find the secret at the corresponding depth
            copath_idx = copath_pubkey_ids.index(copath_pubkey_id)
            target_depth = copath_idx + 1

            matching_secret = None
            for sid, d, p in secrets_to_share:
                if d == target_depth:
                    matching_secret = (sid, d, p)
                    break

            if matching_secret:
                try:
                    shared_id = treekem_secret_shared.create(
                        treekem_secret_id=matching_secret[0],
                        recipient_pubkey_id=copath_pubkey_id,
                        source_update_id=None,  # Will be set after update is created
                        peer_id=peer_id,
                        peer_shared_id=peer_shared_id,
                        t_ms=t_ms,
                        db=db,
                    )
                    shared_ids.append(shared_id)
                    t_ms += 1
                except Exception as e:
                    log.warning(f"create_update_path: failed to share secret at depth {target_depth}: {e}")

    # Step 4: Create the update event
    # The leaf pubkey is the last one created (deepest depth)
    leaf_pubkey_shared_id = path_pubkeys[-1][1] if path_pubkeys else None

    if not leaf_pubkey_shared_id:
        raise ValueError("No leaf pubkey created - cannot create update")

    update_id = create(
        leaf_pubkey_shared_id=leaf_pubkey_shared_id,
        removal_epoch_id=removal_epoch_id,
        base_update_id=base_update_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,
        db=db,
    )

    log.info(f"treekem_update.create_update_path() created update_id={update_id[:20]}...")

    return {
        'treekem_update_id': update_id,
        'path_secrets': path_secrets,
        'path_pubkeys': path_pubkeys,
        'shared_ids': shared_ids,
    }


def create_update_path_dh(
    peer_id: str,
    peer_shared_id: str,
    removal_epoch_id: str | None,
    base_update_id: str | None,
    active_members: list[str],
    t_ms: int,
    db: Any,
) -> dict[str, Any]:
    """Create TreeKEM update path using DH key exchange (BeeKEM style).

    This is the DH-based update path that enables O(1) root convergence.
    Key insight: DH(a, B) = DH(b, A) - both siblings compute the SAME parent secret!

    Algorithm:
    1. Generate new X25519 keypair at leaf
    2. For each level from leaf to root:
       a. Get sibling's resolution (may be multiple pubkeys if blank)
       b. For each resolved pubkey:
          - Compute shared_secret = DH(my_private_key, sibling_pubkey)
          - Derive parent_secret = KDF(shared_secret)
       c. Store parent_secret, derive parent_pubkey
       d. Encrypt parent_secret for each resolved member
    3. Publish all new public keys via treekem_pubkey_shared
    4. Publish encrypted secrets via treekem_secret_shared

    Args:
        peer_id: Local peer ID (owner)
        peer_shared_id: Public peer ID (signer identity)
        removal_epoch_id: Current removal epoch (for forward secrecy) or None
        base_update_id: Previous update this builds on (None for first)
        active_members: List of active member peer_shared_ids for resolution
        t_ms: Timestamp
        db: Database connection

    Returns:
        Dict with:
        - treekem_update_id: The update event ID
        - path_secrets: List of (treekem_secret_id, depth, path_prefix) tuples
        - path_pubkeys: List of (treekem_pubkey_id, treekem_pubkey_shared_id, depth) tuples
        - shared_ids: List of treekem_secret_shared event IDs
    """
    from core import treekem
    from events.group import treekem_pubkey, treekem_pubkey_shared, treekem_secret, treekem_secret_shared

    log.info(f"treekem_update.create_update_path_dh() peer={peer_shared_id[:20]}..., "
             f"active_members={len(active_members)}")

    # Calculate tree depth from number of active members
    import math
    if len(active_members) <= 1:
        path_depth = 1
    else:
        path_depth = max(1, int(math.ceil(math.log2(len(active_members)))))

    # Limit to reasonable depth
    path_depth = min(path_depth, treekem.MAX_TREE_DEPTH)

    # Step 1: Generate new X25519 keypair at leaf
    # This creates the local keypair event
    leaf_depth = path_depth
    leaf_prefix = treekem.get_path_prefix(peer_shared_id, leaf_depth)

    # Create leaf keypair (X25519 for DH)
    leaf_pubkey_id, leaf_pubkey_shared_id, leaf_private_key = treekem_pubkey.create(
        depth=leaf_depth,
        path_prefix=leaf_prefix,
        parent_pubkey_shared_id=None,  # Will be updated after creating parent
        removal_epoch_id=removal_epoch_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,
        db=db,
    )
    t_ms += 1

    # Track our path from leaf to root
    path_secrets: list[tuple[str, int, bytes]] = []
    path_pubkeys: list[tuple[str, str, int]] = [(leaf_pubkey_id, leaf_pubkey_shared_id, leaf_depth)]
    shared_ids: list[str] = []

    # Current private key starts at leaf
    current_private_key = leaf_private_key
    current_depth = leaf_depth

    # Step 2: Walk up from leaf to root, computing DH at each level
    while current_depth > 0:
        current_prefix = treekem.get_path_prefix(peer_shared_id, current_depth)
        parent_depth = current_depth - 1
        parent_prefix = treekem.get_path_prefix(peer_shared_id, parent_depth)

        # Get sibling's position
        sibling_depth = current_depth
        sibling_prefix = treekem.get_copath_prefix(peer_shared_id, current_depth)

        # Get sibling's resolution (may be multiple pubkeys if sibling is blank)
        resolution = treekem_pubkey_shared.get_resolution(
            depth=sibling_depth,
            path_prefix=sibling_prefix,
            max_depth=path_depth,
            recorded_by=peer_id,
            db=db,
            removal_epoch_id=removal_epoch_id,
        )

        if not resolution:
            # No siblings - we're alone in this subtree
            # Generate a random parent secret instead
            parent_secret = crypto.generate_secret()
        else:
            # Compute DH with first resolved sibling to get parent secret
            # All siblings in resolution will compute the same parent via symmetric DH
            # Note: Keys are Ed25519, converted to X25519 internally for DH
            sibling_pubkey = resolution[0]
            shared_secret = crypto.ecdh_shared_secret(
                current_private_key,
                sibling_pubkey['public_key'],
                keys_are_x25519=False  # Ed25519 keys, will be converted
            )
            parent_secret = crypto.derive_dh_path_secret(
                shared_secret,
                current_depth,
                current_prefix
            )

        # Create parent secret event
        parent_secret_id = treekem_secret.create_with_material(
            depth=parent_depth,
            path_prefix=parent_prefix,
            key_material=parent_secret,
            peer_id=peer_id,
            t_ms=t_ms,
            db=db,
        )
        path_secrets.append((parent_secret_id, parent_depth, parent_prefix))
        t_ms += 1

        # Create parent keypair - Ed25519 for compatibility with crypto.seal()
        parent_private_key, parent_public_key = crypto.generate_keypair()

        parent_pubkey_id = treekem_pubkey.create_from_material(
            public_key=parent_public_key,
            private_key=parent_private_key,
            peer_id=peer_id,
            t_ms=t_ms,
            db=db,
        )
        t_ms += 1

        # Create pubkey_shared for parent
        parent_pubkey_shared_id = treekem_pubkey_shared.create(
            treekem_pubkey_id=parent_pubkey_id,
            depth=parent_depth,
            path_prefix=parent_prefix,
            parent_pubkey_shared_id=None if parent_depth == 0 else None,  # Will chain later
            removal_epoch_id=removal_epoch_id,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,
            db=db,
        )
        path_pubkeys.append((parent_pubkey_id, parent_pubkey_shared_id, parent_depth))
        t_ms += 1

        # Share parent secret to resolved siblings via asymmetric encryption
        # Each resolved member gets the parent secret encrypted to their pubkey
        # (using treekem_secret_shared which wraps via crypto.seal())
        for sibling in resolution:
            try:
                # Create secret_shared using the sibling's pubkey as recipient
                sibling_pubkey_id = crypto.b64encode(sibling['id'])
                shared_id = treekem_secret_shared.create(
                    treekem_secret_id=parent_secret_id,
                    recipient_pubkey_id=sibling_pubkey_id,
                    source_update_id=None,  # Will be set after update created
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    t_ms=t_ms,
                    db=db,
                )
                shared_ids.append(shared_id)
                t_ms += 1
            except Exception as e:
                log.warning(f"create_update_path_dh: failed to share to sibling at depth {current_depth}: {e}")

        # Move up to parent for next iteration
        current_private_key = parent_private_key
        current_depth = parent_depth

    # Reverse path_secrets so depth=0 (root) is first
    path_secrets.reverse()
    path_pubkeys.reverse()

    # Step 3: Create the update event
    # The leaf pubkey_shared is the reference point
    update_id = create(
        leaf_pubkey_shared_id=leaf_pubkey_shared_id,
        removal_epoch_id=removal_epoch_id,
        base_update_id=base_update_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,
        db=db,
    )

    log.info(f"treekem_update.create_update_path_dh() created update_id={update_id[:20]}...")

    return {
        'treekem_update_id': update_id,
        'path_secrets': path_secrets,
        'path_pubkeys': path_pubkeys,
        'shared_ids': shared_ids,
    }


def update_all_peers_if_needed(t_ms: int, db: Any) -> dict[str, Any]:
    """Update TreeKEM for all local peers that need updates.

    Called by TreeKEMUpdateJob to periodically create updates with join delay.

    Args:
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Dict with stats about updates created
    """
    from core.db import create_unsafe_db
    from events.identity import removal_epoch

    unsafedb = create_unsafe_db(db)
    peers = unsafedb.query("SELECT peer_id FROM local_peers")

    updates_created = 0
    for peer_row in peers:
        peer_id = peer_row['peer_id']

        if should_update(peer_id, t_ms, db):
            try:
                # Get peer_shared_id
                from core.db import create_safe_db
                safedb = create_safe_db(db, recorded_by=peer_id)
                peer_self = safedb.query_one(
                    "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
                    (peer_id, peer_id)
                )
                if not peer_self:
                    continue

                peer_shared_id = peer_self['peer_shared_id']

                # Get current removal epoch
                current_epoch_id = removal_epoch.get_current_epoch(peer_id, db)

                # Get winning base update
                winning = get_winning_update(current_epoch_id, None, peer_id, db)
                base_update_id = winning['treekem_update_id'] if winning else None

                # Get active members for resolution
                from events.group import sender_key
                # Use a dummy group_id - in practice, should get from actual group context
                # For now, get all peers we know about
                active_members = [peer_shared_id]  # At minimum, include ourselves

                # Create DH-based update
                create_update_path_dh(
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    removal_epoch_id=current_epoch_id,
                    base_update_id=base_update_id,
                    active_members=active_members,
                    t_ms=t_ms,
                    db=db,
                )
                updates_created += 1

            except Exception as e:
                log.warning(f"update_all_peers_if_needed: failed for peer {peer_id[:20]}...: {e}")

    return {'updates_created': updates_created}


def should_update(peer_id: str, t_ms: int, db: Any) -> bool:
    """Check if a peer should create a TreeKEM update.

    Implements join delay and rotation interval logic.

    Args:
        peer_id: Local peer ID
        t_ms: Current timestamp
        db: Database connection

    Returns:
        True if peer should update
    """
    from core.db import create_safe_db

    JOIN_DELAY_MS = 10_000  # 10 seconds
    ROTATION_INTERVAL_MS = 300_000  # 5 minutes

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get peer's join time and peer_shared_id
    peer_self = safedb.query_one(
        "SELECT peer_shared_id, recorded_at FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (peer_id, peer_id)
    )
    if not peer_self:
        return False

    peer_shared_id = peer_self['peer_shared_id']
    join_time = peer_self['recorded_at']

    if t_ms - join_time < JOIN_DELAY_MS:
        return False  # Wait for sync to catch up

    # Check if we updated recently (author_peer_id is peer_shared_id)
    latest = get_latest_update_by_author(peer_shared_id, peer_id, db)
    if latest and t_ms - latest['recorded_at'] < ROTATION_INTERVAL_MS:
        return False

    return True


def get_latest_update_by_author(
    author_peer_id: str,
    recorded_by: str,
    db: Any,
) -> dict[str, Any] | None:
    """Get the most recent update by a specific author.

    Args:
        author_peer_id: Author's peer_shared_id
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Latest update record or None
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_one(
        """SELECT treekem_update_id, author_peer_id, leaf_pubkey_shared_id,
                  removal_epoch_id, base_update_id, created_at, recorded_at
           FROM treekem_updates
           WHERE author_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC LIMIT 1""",
        (author_peer_id, recorded_by)
    )
