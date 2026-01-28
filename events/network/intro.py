"""Intro event type (facilitate hole punching between peers).

An initiator peer introduces two other peers to each other.
This allows them to exchange hole punch packets to establish direct communication
through NAT.

Usage:
  - Alice creates intro event for Bob and Charlie
  - Bob and Charlie receive the intro
  - Bob and Charlie trigger hole punch: both send packets to each other
  - NAT mappings are established, allowing direct communication
"""

# Registry metadata
EVENT_TYPE = 'network_intro'
SHAREABLE = True  # Intros sync for NAT traversal
PROJECTION_TABLE = None

# Intros are time-sensitive; drop if too old on receipt.
INTRO_TTL_MS = 30_000

from typing import Any, Optional, List
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp
from events.identity import peer_shared

log = logging.getLogger(__name__)



def _wire_shadow_network_intro(peer1_id: str, peer2_id: str) -> None:
    """Validate network_intro fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_network_intro_plaintext(
        peer1_id=crypto.b64decode(peer1_id),
        peer2_id=crypto.b64decode(peer2_id),
    )
    decoded = wire_format.decode_network_intro_plaintext(plaintext)
    if decoded["peer1_id"] != crypto.b64decode(peer1_id):
        raise ValueError("wire shadow decode peer1_id mismatch")
# v2 event specification - signed by peer_shared, no deps
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


def is_stale_intro(created_at: int | None, recorded_at: int | None) -> bool:
    """Return True when an intro is too old to act on."""
    if not isinstance(created_at, int) or not isinstance(recorded_at, int):
        return False
    return (recorded_at - created_at) > INTRO_TTL_MS


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for intro events."""
    event_data = ctx.event_data

    signed_by = event_data.get('signed_by')
    peer1_id = event_data.get('peer1_id')
    peer2_id = event_data.get('peer2_id')
    created_at = event_data.get('created_at')

    if not all([signed_by, peer1_id, peer2_id, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_network_intro(peer1_id, peer2_id)

    if is_stale_intro(created_at, ctx.recorded_at):
        return ProjectorResult(writes=tuple(), valid_event=True)

    writes = (
        WriteOp(
            op='insert',
            table='pending_intros',
            values={
                'intro_id': ctx.event_id,
                'initiator_peer_id': signed_by,
                'peer1_id': peer1_id,
                'peer2_id': peer2_id,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
                'processed': False,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(
    initiator_peer_id: str,
    peer1_id: str,
    peer2_id: str,
    t_ms: int,
    db: Any
) -> str:
    """Create intro event introducing two peers to each other.

    Args:
        initiator_peer_id: Peer creating the intro (e.g., Alice)
        peer1_id: First peer being introduced (e.g., Bob)
        peer2_id: Second peer being introduced (e.g., Charlie)
        t_ms: Timestamp
        db: Database connection

    Returns:
        intro_id: Event ID of the created intro event
    """
    # Get initiator's peer_shared_id for signing (this is the public identity)
    self_identity = peer_shared.get_self(initiator_peer_id, db)
    if not self_identity:
        log.warning(f"intro.create() no peer_shared_id for initiator {initiator_peer_id[:20]}...")
        raise ValueError(f"No peer_shared_id for initiator")

    initiator_peer_shared_id = self_identity['peer_shared_id']

    log.info(
        f"intro.create() {initiator_peer_shared_id[:20]}... introducing "
        f"{peer1_id[:20]}... and {peer2_id[:20]}..."
    )

    # Sign with initiator's private key
    from events.identity import peer
    private_key = peer.get_private_key(initiator_peer_id, initiator_peer_id, db)
    _wire_shadow_network_intro(peer1_id, peer2_id)

    blob = wire_format.encode_network_intro_wire_event(
        peer1_id_b64=peer1_id,
        peer2_id_b64=peer2_id,
        signed_by_b64=initiator_peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )
    intro_id = store.event(blob, initiator_peer_id, t_ms, db)

    log.info(f"intro.create() created intro_id={intro_id[:20]}...")
    return intro_id


def get_pending_intros(recorded_by: str, db: Any) -> List[dict[str, Any]]:
    """Get all pending intros for a peer (not yet processed).

    Args:
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of dicts with keys: intro_id, initiator_peer_id, peer1_id, peer2_id, created_at
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    rows = safedb.query(
        """SELECT intro_id, initiator_peer_id, peer1_id, peer2_id, created_at
           FROM pending_intros
           WHERE recorded_by = ? AND processed = FALSE
           ORDER BY created_at ASC""",
        (recorded_by,)
    )

    return [
        {
            'intro_id': row['intro_id'],
            'initiator_peer_id': row['initiator_peer_id'],
            'peer1_id': row['peer1_id'],
            'peer2_id': row['peer2_id'],
            'created_at': row['created_at']
        }
        for row in rows
    ]


def mark_processed(intro_id: str, recorded_by: str, db: Any) -> None:
    """Mark an intro as processed (hole punch was attempted).

    Args:
        intro_id: Event ID of the intro
        recorded_by: Local peer ID
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """UPDATE pending_intros SET processed = TRUE
           WHERE intro_id = ? AND recorded_by = ?""",
        (intro_id, recorded_by)
    )
    log.debug(f"intro.mark_processed() marked intro_id={intro_id[:20]}... as processed")


def get_intros_for_peer(peer_id: str, recorded_by: str, db: Any) -> List[dict[str, Any]]:
    """Get intros where a specific peer is involved (either peer1 or peer2).

    Args:
        peer_id: The peer we're looking for
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of intro dicts where peer_id is involved
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    rows = safedb.query(
        """SELECT intro_id, initiator_peer_id, peer1_id, peer2_id, created_at
           FROM pending_intros
           WHERE recorded_by = ? AND processed = FALSE
           AND (peer1_id = ? OR peer2_id = ?)
           ORDER BY created_at ASC""",
        (recorded_by, peer_id, peer_id)
    )

    return [
        {
            'intro_id': row['intro_id'],
            'initiator_peer_id': row['initiator_peer_id'],
            'peer1_id': row['peer1_id'],
            'peer2_id': row['peer2_id'],
            'created_at': row['created_at']
        }
        for row in rows
    ]
