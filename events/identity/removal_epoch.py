"""TreeKEM removal_epoch event type (tracks user/peer removals for forward secrecy).

Removal epochs form a DAG that tracks which users/peers have been removed from
the network. Each removal epoch references the users/peers being removed and
parent epochs (for convergence). This enables:

1. Forward secrecy: Keys shared under a removal epoch are invalid if the epoch
   has been superseded by a new removal.
2. Partition healing: Multiple concurrent removal epochs can converge by
   referencing each other as parents.
"""

# Registry metadata
EVENT_TYPE = 'removal_epoch'
SHAREABLE = True  # Syncs to peers
PROJECTION_TABLE = ('removal_epochs', 'epoch_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Maximum number of removal refs and parent epochs in a single event
MAX_REMOVAL_REFS = 8
MAX_PARENT_EPOCHS = 4


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
    """Pure projector for removal_epoch events.

    Creates entries in removal_epochs and removal_epoch_refs tables.
    """
    event_data = ctx.event_data

    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')
    removed_peer_id = event_data.get('removed_peer_id')  # Single peer being removed
    removed_user_id = event_data.get('removed_user_id')  # Optional user ID
    parent_epoch_id = event_data.get('parent_epoch_id')  # Optional parent epoch

    if not signed_by or created_at is None or not removed_peer_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = []

    # Insert main epoch record
    writes.append(WriteOp(
        op='insert',
        table='removal_epochs',
        values={
            'epoch_id': ctx.event_id,
            'signed_by': signed_by,
            'removed_peer_id': removed_peer_id,
            'removed_user_id': removed_user_id,
            'parent_epoch_id': parent_epoch_id,
            'created_at': created_at,
            'recorded_at': ctx.recorded_at,
        },
    ))

    # Insert removal reference for the peer
    writes.append(WriteOp(
        op='insert',
        table='removal_epoch_refs',
        values={
            'epoch_id': ctx.event_id,
            'removed_id': removed_peer_id,
        },
    ))

    # If there's also a user being removed, add that ref too
    if removed_user_id:
        writes.append(WriteOp(
            op='insert',
            table='removal_epoch_refs',
            values={
                'epoch_id': ctx.event_id,
                'removed_id': removed_user_id,
            },
        ))

    # Insert parent epoch reference if present
    if parent_epoch_id:
        writes.append(WriteOp(
            op='insert',
            table='removal_epoch_parents',
            values={
                'epoch_id': ctx.event_id,
                'parent_epoch_id': parent_epoch_id,
            },
        ))

    return ProjectorResult(writes=tuple(writes), valid_event=True)


def create(peer_id: str, peer_shared_id: str, removed_peer_id: str,
           removed_user_id: str | None, parent_epoch_id: str | None,
           t_ms: int, db: Any) -> str:
    """Create a new removal epoch.

    Args:
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (signer identity)
        removed_peer_id: The peer being removed
        removed_user_id: Optional user ID being removed (if user-level removal)
        parent_epoch_id: Optional parent epoch ID (for DAG convergence)
        t_ms: Timestamp
        db: Database connection

    Returns:
        epoch_id: The event ID
    """
    log.info(f"removal_epoch.create() removing peer={removed_peer_id}, user={removed_user_id}, parent={parent_epoch_id}, t_ms={t_ms}")

    # Get private key for signing
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wire event
    blob = wire_format.encode_removal_epoch_wire_event(
        removed_peer_id_b64=removed_peer_id,
        removed_user_id_b64=removed_user_id,
        parent_epoch_id_b64=parent_epoch_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )

    # Store event
    epoch_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"removal_epoch.create() created epoch_id={epoch_id}")

    return epoch_id


def get_current_epoch(peer_id: str, db: Any) -> str | None:
    """Get the current (most recent head) removal epoch.

    Returns the epoch ID of the most recent removal epoch that has no
    children (i.e., is a head of the DAG).

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        epoch_id or None if no epochs exist
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Find epochs that are not parents of any other epoch (DAG heads)
    row = safedb.query_one(
        """SELECT e.epoch_id FROM removal_epochs e
           WHERE e.recorded_by = ?
           AND NOT EXISTS (
               SELECT 1 FROM removal_epoch_parents p
               WHERE p.parent_epoch_id = e.epoch_id AND p.recorded_by = ?
           )
           ORDER BY e.created_at DESC LIMIT 1""",
        (peer_id, peer_id)
    )

    return row['epoch_id'] if row else None


def get_epoch_heads(peer_id: str, db: Any) -> list[str]:
    """Get all current heads of the removal epoch DAG.

    Returns all epoch IDs that have no children (for convergence handling).

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of epoch IDs
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    rows = safedb.query(
        """SELECT e.epoch_id FROM removal_epochs e
           WHERE e.recorded_by = ?
           AND NOT EXISTS (
               SELECT 1 FROM removal_epoch_parents p
               WHERE p.parent_epoch_id = e.epoch_id AND p.recorded_by = ?
           )
           ORDER BY e.created_at DESC""",
        (peer_id, peer_id)
    )

    return [row['epoch_id'] for row in rows]


def is_removed(entity_id: str, epoch_id: str | None, peer_id: str, db: Any) -> bool:
    """Check if an entity (user/peer) was removed as of a given epoch.

    Traverses the epoch DAG to check if the entity appears in any epoch
    reachable from the given epoch (including the epoch itself).

    Args:
        entity_id: The user or peer ID to check
        epoch_id: The epoch to check from (None = no removals)
        peer_id: Local peer ID
        db: Database connection

    Returns:
        True if the entity was removed
    """
    if epoch_id is None:
        return False

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Check if entity is directly removed in this epoch or any ancestor
    # Uses recursive CTE to traverse the DAG
    row = safedb.query_one(
        """WITH RECURSIVE epoch_chain(eid) AS (
               SELECT ?
               UNION ALL
               SELECT p.parent_epoch_id
               FROM removal_epoch_parents p, epoch_chain c
               WHERE p.epoch_id = c.eid AND p.recorded_by = ?
           )
           SELECT 1 FROM removal_epoch_refs r, epoch_chain c
           WHERE r.epoch_id = c.eid AND r.removed_id = ? AND r.recorded_by = ?
           LIMIT 1""",
        (epoch_id, peer_id, entity_id, peer_id)
    )

    return row is not None


def get_removed_entities(epoch_id: str, peer_id: str, db: Any) -> list[str]:
    """Get all entities removed as of a given epoch.

    Args:
        epoch_id: The epoch to check
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of removed entity IDs
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get all entities removed in this epoch or any ancestor
    rows = safedb.query(
        """WITH RECURSIVE epoch_chain(eid) AS (
               SELECT ?
               UNION ALL
               SELECT p.parent_epoch_id
               FROM removal_epoch_parents p, epoch_chain c
               WHERE p.epoch_id = c.eid AND p.recorded_by = ?
           )
           SELECT DISTINCT r.removed_id FROM removal_epoch_refs r, epoch_chain c
           WHERE r.epoch_id = c.eid AND r.recorded_by = ?""",
        (epoch_id, peer_id, peer_id)
    )

    return [row['removed_id'] for row in rows]


def get_epoch(epoch_id: str, peer_id: str, db: Any) -> dict[str, Any] | None:
    """Get a removal epoch by ID.

    Args:
        epoch_id: The epoch ID
        peer_id: Local peer ID
        db: Database connection

    Returns:
        Epoch record with removed_peer_id, removed_user_id, parent_epoch_id, or None
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    epoch = safedb.query_one(
        "SELECT * FROM removal_epochs WHERE epoch_id = ? AND recorded_by = ?",
        (epoch_id, peer_id)
    )

    if not epoch:
        return None

    return dict(epoch)


def list(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all removal epochs.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of epoch records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT * FROM removal_epochs
           WHERE recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id,)
    ))
