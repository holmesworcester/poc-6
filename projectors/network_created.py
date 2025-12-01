"""Network created projector (marks a peer as network creator).

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Only projects if recorded_by == peer_id (own network_created event).
Uses apply_result since network_creators is subjective (has recorded_by).
"""

from typing import TypedDict
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class NetworkCreatedEventData(TypedDict):
    type: str
    peer_id: str
    created_at: int


class NetworkCreatedInput(TypedDict):
    event_id: str
    event_data: NetworkCreatedEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plain JSON (local-only)
    "signer_type": "none",  # Not signed (local-only)
    "dependencies": [],
    "tables": ["network_creators"],  # Subjective table (has recorded_by)
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: NetworkCreatedInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Outputs network_creators row. Use apply_result().
    Only projects if recorded_by == peer_id (own event).
    """
    event_data: NetworkCreatedEventData = input_dict["event_data"]
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
# TEST BUILDERS - compose these to create test inputs (no DB required)
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
