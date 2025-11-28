"""Address projector.

Peer's network address for direct communication.
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult
import logging

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
