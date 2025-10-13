"""Network created event type - marks peer as network creator (self-bootstrapped)."""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project network_created event into bootstrap_status.

    Marks this peer as network creator.
    """
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"network_created.project() blob not found for event_id={event_id}")
        return None

    # Parse JSON (signed plaintext)
    event_data = crypto.parse_json(blob)

    peer_id = event_data.get('peer_id')

    if not peer_id:
        log.warning(f"network_created.project() missing peer_id")
        return None

    # Only project if this is our own network_created event
    if recorded_by != peer_id:
        log.debug(f"network_created.project() skipping foreign network_created event")
        return event_id

    # Mark this peer as network creator (subjective table, use safedb)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR REPLACE INTO bootstrap_status
           (peer_id, recorded_by, created_network, joined_network)
           VALUES (?, ?, 1, 0)""",
        (peer_id, recorded_by)
    )

    log.info(f"network_created.project() marked {peer_id[:20]}... as network creator")

    return event_id
