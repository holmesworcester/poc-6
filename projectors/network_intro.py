"""Network intro projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Handles peer introduction for hole punching (NAT traversal).
"""

from typing import TypedDict
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class NetworkIntroEventData(TypedDict):
    type: str
    initiator_peer_id: str
    peer1_id: str
    peer2_id: str
    created_at: int


class NetworkIntroInput(TypedDict):
    event_id: str
    event_data: NetworkIntroEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plain JSON, not encrypted
    "signer_type": "none",  # TODO: Should be "peer_shared" - add signing to create()
    "dependencies": [],  # No dependencies
    "tables": ["pending_intros"],
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: NetworkIntroInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Outputs intro to pending_intros table for hole punch processing.
    """
    event_id = input_dict["event_id"]
    event_data: NetworkIntroEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    initiator_peer_id = event_data.get("initiator_peer_id")
    peer1_id = event_data.get("peer1_id")
    peer2_id = event_data.get("peer2_id")
    created_at = event_data.get("created_at")

    if not all([initiator_peer_id, peer1_id, peer2_id, created_at]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Output: pending_intros row
    row = {
        "intro_id": event_id,
        "initiator_peer_id": initiator_peer_id,
        "peer1_id": peer1_id,
        "peer2_id": peer2_id,
        "created_at": created_at,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "processed": False,
    }

    return ProjectorResult(
        valid=True,
        tables={"pending_intros": [row]},
    )


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    initiator_peer_id: str = "alice_123",
    peer1_id: str = "bob_123",
    peer2_id: str = "charlie_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "network_intro",
        "initiator_peer_id": initiator_peer_id,
        "peer1_id": peer1_id,
        "peer2_id": peer2_id,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "intro_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_789",
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
