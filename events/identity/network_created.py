"""Network created event type - marks peer as network creator (self-bootstrapped).

SPEC - declarative metadata for generic resolver
project() - pure function: input_dict -> ProjectorResult

API functions:
    project_event(event_id, recorded_by, recorded_at, db) -> str | None
"""
from typing import Any, TypedDict
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class NetworkCreatedEventData(TypedDict):
    type: str
    peer_id: str
    created_at: int


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,
    "signer_type": "none",  # Local-only, no signature
    "dependencies": [],
    "tables": ["network_creators"],
    "generic_dispatch": True,
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result.

    Outputs network_creators row. Use apply_result().
    Only projects if recorded_by == peer_id (own event).
    """
    from projection import ProjectorResult

    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]

    peer_id = event_data.get("peer_id")

    if not peer_id:
        return ProjectorResult(valid=False, reason="missing required field: peer_id")

    # Only project if this is our own network_created event
    if recorded_by != peer_id:
        # Foreign network_created event - valid but no output
        return ProjectorResult(valid=True, tables={})

    # Output: network_creators row (subjective table)
    row = {
        "peer_id": peer_id,
        "recorded_by": recorded_by,
    }

    return ProjectorResult(
        valid=True,
        tables={"network_creators": [row]},
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    peer_id: str = "peer_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "network_created",
        "peer_id": peer_id,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "nc_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_123",  # Must match peer_id for projection to occur
    recorded_at: int = 1000001,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }


# ============================================================================
# API FUNCTIONS
# ============================================================================

def project_event(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project network_created event using pure projector.

    Uses apply_result since network_creators is subjective (has recorded_by).
    """
    from projection import resolve, apply_result

    input_dict = resolve("network_created", event_id, recorded_by, recorded_at, db)
    if not input_dict:
        log.warning(f"network_created.project_event() resolve failed for event_id={event_id}")
        return None

    result = project(input_dict)

    if not result.valid:
        log.warning(f"network_created.project_event() rejected: {result.reason}")
        return None

    # Apply to subjective table (if any output)
    if result.tables:
        apply_result(result, recorded_by, recorded_at, db)
        peer_id = input_dict["event_data"].get('peer_id')
        log.info(f"network_created.project_event() marked {peer_id[:20]}... as network creator")

    return event_id
