"""Sync connect projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Note: sync_connections is a DEVICE-WIDE table (not subjective).
The pure projector validates and extracts data; the wrapper handles
the device-wide write to sync_connections.
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class SyncConnectEventData(TypedDict):
    type: str
    peer_id: str
    signed_by: str
    address: str
    port: int
    response_transit_key_id: str
    response_transit_key: str
    invite_id: NotRequired[str]
    invite_signature: NotRequired[str]
    created_at: int


class SyncConnectInput(TypedDict):
    event_id: str
    event_data: SyncConnectEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict
    signature_valid: bool  # From polymorphic signature verification


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": True,  # Transit-wrapped
    "signer_type": "peer_shared_polymorphic",  # Can be peer or invite signature
    "dependencies": [],  # No blocking dependencies
    "tables": [],  # Device-wide table handled by wrapper
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: SyncConnectInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Validates sync_connect and extracts connection info.
    Device-wide table write handled by wrapper.
    """
    event_data: SyncConnectEventData = input_dict["event_data"]
    signature_valid = input_dict.get("signature_valid", False)

    peer_shared_id = event_data.get("signed_by")
    response_transit_key_id = event_data.get("response_transit_key_id")
    response_transit_key = event_data.get("response_transit_key")
    address = event_data.get("address")
    port = event_data.get("port")

    if not all([peer_shared_id, response_transit_key_id, response_transit_key]):
        return ProjectorResult(valid=False, reason="missing required fields")

    if not signature_valid:
        return ProjectorResult(valid=False, reason="signature verification failed")

    # Valid - wrapper will handle device-wide table write
    return ProjectorResult(valid=True)


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    peer_id: str = "peer_123",
    signed_by: str = "peer_shared_123",
    address: str = "127.0.0.1",
    port: int = 8000,
    response_transit_key_id: str = "key_123",
    response_transit_key: str = "base64key==",
    invite_id: str | None = None,
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    data = {
        "type": "sync_connect",
        "peer_id": peer_id,
        "signed_by": signed_by,
        "address": address,
        "port": port,
        "response_transit_key_id": response_transit_key_id,
        "response_transit_key": response_transit_key,
        "created_at": created_at,
    }
    if invite_id:
        data["invite_id"] = invite_id
    return data


def make_input(
    event_id: str = "conn_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    signature_valid: bool = True,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
        "signature_valid": signature_valid,
    }
