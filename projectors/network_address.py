"""Network address projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Handles peer endpoint observations (e.g., "Alice saw Bob at 203.0.113.5:42000").
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class NetworkAddressEventData(TypedDict):
    type: str
    observed_peer_id: str
    observed_by_peer_id: str
    ip: str
    port: int
    created_at: int


class NetworkAddressInput(TypedDict):
    event_id: str
    event_data: NetworkAddressEventData
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
    "tables": ["network_addresses"],
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: NetworkAddressInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Outputs network address observation to network_addresses table.
    """
    event_id = input_dict["event_id"]
    event_data: NetworkAddressEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    observed_peer_id = event_data.get("observed_peer_id")
    observed_by_peer_id = event_data.get("observed_by_peer_id")
    ip = event_data.get("ip")
    port = event_data.get("port")
    created_at = event_data.get("created_at")

    if not all([observed_peer_id, observed_by_peer_id, ip, port, created_at]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Output: network_addresses row
    row = {
        "address_id": event_id,
        "observed_peer_id": observed_peer_id,
        "observed_by_peer_id": observed_by_peer_id,
        "ip": ip,
        "port": port,
        "created_at": created_at,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(
        valid=True,
        tables={"network_addresses": [row]},
    )


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    observed_peer_id: str = "peer_123",
    observed_by_peer_id: str = "peer_456",
    ip: str = "203.0.113.5",
    port: int = 42000,
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "network_address",
        "observed_peer_id": observed_peer_id,
        "observed_by_peer_id": observed_by_peer_id,
        "ip": ip,
        "port": port,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "addr_123",
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
