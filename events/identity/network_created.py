"""Network created event type - marks peer as network creator (self-bootstrapped)."""
from typing import Any
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project network_created event using pure projector.

    Uses apply_result since network_creators is subjective (has recorded_by).
    """
    from projectors import apply_result
    from projectors import network_created as nc_projector

    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"network_created.project() blob not found for event_id={event_id}")
        return None

    # Parse JSON (signed plaintext)
    event_data = crypto.parse_json(blob)

    # Build input dict for pure projector
    input_dict = {
        "event_id": event_id,
        "event_data": event_data,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }

    # Call pure projector
    result = nc_projector.project(input_dict)

    if not result.valid:
        log.warning(f"network_created.project() rejected: {result.reason}")
        return None

    # Apply to subjective table (if any output)
    if result.tables:
        apply_result(result, recorded_by, recorded_at, db)
        peer_id = event_data.get('peer_id')
        log.info(f"network_created.project() marked {peer_id[:20]}... as network creator")

    return event_id
