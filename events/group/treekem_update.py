"""TreeKEM Phase 2: treekem_update event type (update orchestration event).

This is the orchestration event that ties together an update path:
- References the author's new root pubkey
- Links to parent update for ordering
- Provides removal_epoch_id for forward secrecy

For concurrent updates, the update with the lowest treekem_update_id wins,
providing deterministic convergence across all peers.
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
    'optional': {},
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
    root_pubkey_id = event_data.get('root_pubkey_id')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')
    removal_epoch_id = event_data.get('removal_epoch_id')
    base_update_id = event_data.get('base_update_id')

    if not author_peer_id or not root_pubkey_id or not signed_by or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Insert the update record
    writes = (
        WriteOp(
            op='insert',
            table='treekem_updates',
            values={
                'treekem_update_id': ctx.event_id,
                'author_peer_id': author_peer_id,
                'root_pubkey_id': root_pubkey_id,
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
    root_pubkey_id: str,
    removal_epoch_id: str | None,
    base_update_id: str | None,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create a new treekem_update event.

    Args:
        root_pubkey_id: The root pubkey ID for this update
        removal_epoch_id: Current removal epoch (for forward secrecy)
        base_update_id: Previous update this builds on (None for first)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (signer identity)
        t_ms: Timestamp
        db: Database connection

    Returns:
        treekem_update_id: The update event ID
    """
    log.info(f"treekem_update.create() peer_id={peer_id}, root_pubkey_id={root_pubkey_id[:20]}...")

    # Get private key for signing
    from events.identity import peer
    signing_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wire event
    blob = wire_format.encode_treekem_update_wire_event(
        author_peer_id_b64=peer_shared_id,
        removal_epoch_id_b64=removal_epoch_id,
        base_update_id_b64=base_update_id,
        root_pubkey_id_b64=root_pubkey_id,
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
        """SELECT treekem_update_id, author_peer_id, root_pubkey_id,
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
            """SELECT treekem_update_id, author_peer_id, root_pubkey_id,
                      removal_epoch_id, base_update_id, created_at
               FROM treekem_updates
               WHERE removal_epoch_id = ? AND recorded_by = ?
               ORDER BY created_at DESC LIMIT 1""",
            (removal_epoch_id, recorded_by)
        )
    else:
        return safedb.query_one(
            """SELECT treekem_update_id, author_peer_id, root_pubkey_id,
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
            """SELECT treekem_update_id, author_peer_id, root_pubkey_id,
                      removal_epoch_id, base_update_id, created_at
               FROM treekem_updates
               WHERE base_update_id IS NULL AND removal_epoch_id IS NULL AND recorded_by = ?
               ORDER BY treekem_update_id ASC""",
            (recorded_by,)
        )
    elif base_update_id is None:
        updates = safedb.query(
            """SELECT treekem_update_id, author_peer_id, root_pubkey_id,
                      removal_epoch_id, base_update_id, created_at
               FROM treekem_updates
               WHERE base_update_id IS NULL AND removal_epoch_id = ? AND recorded_by = ?
               ORDER BY treekem_update_id ASC""",
            (removal_epoch_id, recorded_by)
        )
    elif removal_epoch_id is None:
        updates = safedb.query(
            """SELECT treekem_update_id, author_peer_id, root_pubkey_id,
                      removal_epoch_id, base_update_id, created_at
               FROM treekem_updates
               WHERE base_update_id = ? AND removal_epoch_id IS NULL AND recorded_by = ?
               ORDER BY treekem_update_id ASC""",
            (base_update_id, recorded_by)
        )
    else:
        updates = safedb.query(
            """SELECT treekem_update_id, author_peer_id, root_pubkey_id,
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
        """SELECT treekem_update_id, author_peer_id, root_pubkey_id,
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
