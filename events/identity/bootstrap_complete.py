"""Bootstrap complete event type - marks when a joiner receives first sync request from network."""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, t_ms: int, db: Any) -> str:
    """Create local-only bootstrap_complete event.

    This is created when a joiner receives their first sync request from the network,
    indicating they have successfully bootstrapped.

    Args:
        peer_id: The peer who has completed bootstrap
        t_ms: Timestamp
        db: Database connection

    Returns:
        bootstrap_complete event ID
    """
    from events.identity import peer

    # Create event data (local-only, so minimal fields)
    event_data = {
        'type': 'bootstrap_complete',
        'peer_id': peer_id,
        'created_at': t_ms
    }

    # Get peer's private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)
    if not private_key:
        log.error(f"bootstrap_complete.create() no private key for peer {peer_id[:20]}...")
        raise ValueError(f"No private key for peer {peer_id}")

    # Sign the event
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext (local-only)
    canonical = crypto.canonicalize_json(signed_event)
    bootstrap_complete_id = store.event(canonical, peer_id, t_ms, db)

    log.info(f"bootstrap_complete.create() created event {bootstrap_complete_id[:20]}... for peer {peer_id[:20]}...")

    return bootstrap_complete_id


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project bootstrap_complete event into bootstrap_completers table.

    Marks this peer as having received their first sync request (bootstrap acknowledged).
    """
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"bootstrap_complete.project() blob not found for event_id={event_id}")
        return None

    # Parse JSON (signed plaintext)
    event_data = crypto.parse_json(blob)

    peer_id = event_data.get('peer_id')

    if not peer_id:
        log.warning(f"bootstrap_complete.project() missing peer_id")
        return None

    # Only project if this is our own bootstrap_complete event
    if recorded_by != peer_id:
        log.debug(f"bootstrap_complete.project() skipping foreign bootstrap_complete event")
        return event_id

    # Mark this peer as having received sync acknowledgment (subjective table, use safedb)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO bootstrap_completers (peer_id, recorded_by) VALUES (?, ?)""",
        (peer_id, recorded_by)
    )

    log.info(f"bootstrap_complete.project() marked {peer_id[:20]}... as received sync request")

    return event_id
