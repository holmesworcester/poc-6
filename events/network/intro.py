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
from typing import Any, Optional, List
import json
import logging
import crypto
import store
from db import create_safe_db

log = logging.getLogger(__name__)


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
    log.info(
        f"intro.create() {initiator_peer_id[:20]}... introducing "
        f"{peer1_id[:20]}... and {peer2_id[:20]}..."
    )

    # Create event blob (plaintext JSON)
    event_data = {
        'type': 'network_intro',
        'initiator_peer_id': initiator_peer_id,
        'peer1_id': peer1_id,
        'peer2_id': peer2_id,
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store event with recorded wrapper
    intro_id = store.event(blob, initiator_peer_id, t_ms, db)

    log.info(f"intro.create() created intro_id={intro_id[:20]}...")
    return intro_id


def project(intro_id: str, recorded_by: str, recorded_at: int, db: Any) -> Optional[str]:
    """Project intro event into pending_intros table.

    Args:
        intro_id: Event ID of the intro event
        recorded_by: Peer ID that recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection

    Returns:
        intro_id if successful, None otherwise
    """
    log.debug(
        f"intro.project() intro_id={intro_id[:20]}..., "
        f"recorded_by={recorded_by[:20]}..."
    )

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    from db import create_unsafe_db
    unsafedb = create_unsafe_db(db)
    blob = store.get(intro_id, unsafedb)
    if not blob:
        log.warning(f"intro.project() blob not found for intro_id={intro_id}")
        return None

    # Parse JSON
    try:
        event_data = json.loads(blob.decode())
    except Exception as e:
        log.warning(f"intro.project() failed to parse event data: {e}")
        return None

    # Validate event structure
    if event_data.get('type') != 'network_intro':
        log.warning(f"intro.project() wrong type: {event_data.get('type')}")
        return None

    initiator_peer_id = event_data.get('initiator_peer_id')
    peer1_id = event_data.get('peer1_id')
    peer2_id = event_data.get('peer2_id')
    created_at = event_data.get('created_at')

    if not all([initiator_peer_id, peer1_id, peer2_id, created_at]):
        log.warning(f"intro.project() missing required fields")
        return None

    # Insert into pending_intros table
    safedb.execute(
        """INSERT OR REPLACE INTO pending_intros
           (intro_id, initiator_peer_id, peer1_id, peer2_id, created_at, recorded_by, recorded_at, processed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            intro_id,
            initiator_peer_id,
            peer1_id,
            peer2_id,
            created_at,
            recorded_by,
            recorded_at,
            False
        )
    )

    # Mark as valid for this peer
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (intro_id, recorded_by)
    )

    log.info(
        f"intro.project() inserted: {initiator_peer_id[:20]}... introducing "
        f"{peer1_id[:20]}... and {peer2_id[:20]}..."
    )

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
