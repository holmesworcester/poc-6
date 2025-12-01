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
    from projectors import resolve, apply_result
    from projectors import network_created as nc_projector

    input_dict = resolve("network_created", event_id, recorded_by, recorded_at, db)
    if not input_dict:
        log.warning(f"network_created.project() resolve failed for event_id={event_id}")
        return None

    result = nc_projector.project(input_dict)

    if not result.valid:
        log.warning(f"network_created.project() rejected: {result.reason}")
        return None

    # Apply to subjective table (if any output)
    if result.tables:
        apply_result(result, recorded_by, recorded_at, db)
        peer_id = input_dict["event_data"].get('peer_id')
        log.info(f"network_created.project() marked {peer_id[:20]}... as network creator")

    return event_id
