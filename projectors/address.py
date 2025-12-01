"""Address projector.

Peer's network address for direct communication.
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult, CreateResult, BlobSpec, compute_event_id
import logging
import crypto

log = logging.getLogger(__name__)


class AddressEventData(TypedDict):
    type: str
    peer_id: str  # peer_shared_id
    signed_by: str
    ip: str
    port: int
    created_at: int


class AddressInput(TypedDict):
    event_id: str
    event_data: AddressEventData
    recorded_by: str
    recorded_at: int


SPEC = {
    "encrypted": False,
    "signer_type": "peer_shared",
    "dependencies": [],
    "tables": ["addresses"],
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

class AddressCreateDeps(TypedDict):
    """Dependencies for address creation."""
    peer_shared_id: str
    private_key: bytes


def create_pure(
    deps: AddressCreateDeps,
    ip: str,
    port: int,
    t_ms: int,
) -> CreateResult:
    """Pure function to create an address event.

    Address events are signed plaintext announcing a peer's network location.

    Args:
        deps: Resolved dependencies
        ip: IP address
        port: Port number
        t_ms: Timestamp

    Returns:
        CreateResult with address blob
    """
    event_data = {
        'type': 'address',
        'peer_id': deps['peer_shared_id'],
        'signed_by': deps['peer_shared_id'],
        'ip': ip,
        'port': port,
        'created_at': t_ms,
    }

    # Sign the event
    signed_event = crypto.sign_event(event_data, deps['private_key'])

    # Canonicalize (plaintext, no encryption)
    blob = crypto.canonicalize_json(signed_event)
    address_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=address_id, event_type='address')],
        primary_id=address_id,
    )


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: AddressInput) -> ProjectorResult:
    """Pure projection: dict -> result."""
    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    peer_shared_id = event_data.get("peer_id")
    ip = event_data.get("ip")
    port = event_data.get("port")
    created_at = event_data.get("created_at")

    if not all([peer_shared_id, ip, port is not None, created_at]):
        return ProjectorResult(valid=False, reason="Missing required fields")

    row = {
        "address_id": event_id,
        "peer_shared_id": peer_shared_id,
        "ip": ip,
        "port": port,
        "created_at": created_at,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(valid=True, tables={"addresses": [row]})


# Test builders
def make_event_data(
    peer_id: str = "ps_123",
    signed_by: str = "ps_123",
    ip: str = "127.0.0.1",
    port: int = 6100,
    created_at: int = 1000000,
) -> dict:
    return {
        "type": "address",
        "peer_id": peer_id,
        "signed_by": signed_by,
        "ip": ip,
        "port": port,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "addr_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
) -> dict:
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }
