"""Bootstrap complete projector.

Local-only event marking when a joiner receives first sync request from network.
"""

from typing import TypedDict
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


class BootstrapCompleteEventData(TypedDict):
    type: str
    peer_id: str
    created_at: int


class BootstrapCompleteInput(TypedDict):
    event_id: str
    event_data: BootstrapCompleteEventData
    recorded_by: str
    recorded_at: int


SPEC = {
    "encrypted": False,
    "signer_type": "none",  # Local-only, no signature verification
    "dependencies": [],
    "tables": ["bootstrap_completers"],
}


def project(input_dict: BootstrapCompleteInput) -> ProjectorResult:
    """Pure projection: dict -> result."""
    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]

    peer_id = event_data.get("peer_id")

    if not peer_id:
        return ProjectorResult(valid=False, reason="Missing peer_id")

    # Only project our own bootstrap_complete event
    if recorded_by != peer_id:
        # Foreign event - valid but no tables to write
        return ProjectorResult(valid=True, tables={})

    row = {
        "peer_id": peer_id,
        "recorded_by": recorded_by,
    }

    return ProjectorResult(valid=True, tables={"bootstrap_completers": [row]})


# Test builders
def make_event_data(peer_id: str = "peer_123", created_at: int = 1000000) -> dict:
    return {
        "type": "bootstrap_complete",
        "peer_id": peer_id,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "bc_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_123",
    recorded_at: int = 1000001,
) -> dict:
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(peer_id=recorded_by),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }
