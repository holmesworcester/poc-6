"""Network joined projector.

Local-only event marking successful bootstrap with inviter.
"""

from typing import TypedDict
from projectors import ProjectorResult, CreateResult, BlobSpec, compute_event_id
import logging
import crypto

log = logging.getLogger(__name__)


class NetworkJoinedEventData(TypedDict):
    type: str
    peer_id: str
    signed_by: str
    inviter_peer_shared_id: str
    created_at: int


class NetworkJoinedInput(TypedDict):
    event_id: str
    event_data: NetworkJoinedEventData
    recorded_by: str
    recorded_at: int


SPEC = {
    "encrypted": False,
    "signer_type": "none",  # Local-only, no signature verification
    "dependencies": [],
    "tables": ["network_joiners"],
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    # Private key for signing
    "private_key": {"type": "local_peer_key"},
}


# ============================================================================
# CREATE - pure function: deps -> CreateResult
# ============================================================================

class NetworkJoinedCreateDeps(TypedDict):
    """Dependencies for network_joined creation."""
    peer_id: str
    peer_shared_id: str
    private_key: bytes
    inviter_peer_shared_id: str


def create_pure(
    deps: NetworkJoinedCreateDeps,
    t_ms: int,
) -> CreateResult:
    """Pure function to create a network_joined event.

    Marks successful bootstrap with inviter.

    Args:
        deps: Resolved dependencies
        t_ms: Timestamp

    Returns:
        CreateResult with network_joined blob
    """
    event_data = {
        'type': 'network_joined',
        'peer_id': deps['peer_id'],
        'signed_by': deps['peer_shared_id'],
        'inviter_peer_shared_id': deps['inviter_peer_shared_id'],
        'created_at': t_ms,
    }

    # Sign the event
    signed_event = crypto.sign_event(event_data, deps['private_key'])

    # Canonicalize (plaintext, no encryption)
    blob = crypto.canonicalize_json(signed_event)
    network_joined_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=network_joined_id, event_type='network_joined')],
        primary_id=network_joined_id,
    )


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: NetworkJoinedInput) -> ProjectorResult:
    """Pure projection: dict -> result."""
    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]

    peer_id = event_data.get("peer_id")
    inviter_peer_shared_id = event_data.get("inviter_peer_shared_id")

    if not peer_id or not inviter_peer_shared_id:
        return ProjectorResult(valid=False, reason="Missing peer_id or inviter_peer_shared_id")

    # Only project our own network_joined event
    if recorded_by != peer_id:
        return ProjectorResult(valid=True, tables={})

    row = {
        "peer_id": peer_id,
        "recorded_by": recorded_by,
    }

    return ProjectorResult(valid=True, tables={"network_joiners": [row]})


# Test builders
def make_event_data(
    peer_id: str = "peer_123",
    signed_by: str = "ps_123",
    inviter_peer_shared_id: str = "ps_inviter",
    created_at: int = 1000000,
) -> dict:
    return {
        "type": "network_joined",
        "peer_id": peer_id,
        "signed_by": signed_by,
        "inviter_peer_shared_id": inviter_peer_shared_id,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "nj_123",
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
