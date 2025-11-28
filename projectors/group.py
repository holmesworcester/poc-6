"""Group projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Groups have polymorphic signing:
- Network-signed: signed_by == network_id (for all_users group)
- Peer-signed: signed_by == peer_shared_id (for user-created groups)
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class GroupEventData(TypedDict):
    type: str
    name: str
    signed_by: str  # network_id or peer_shared_id
    created_at: int
    key_id: str
    is_main: NotRequired[int]  # 1 if main group, 0 otherwise
    network_id: NotRequired[str]  # Network this group belongs to


class GroupInput(TypedDict):
    event_id: str
    event_data: GroupEventData
    recorded_by: str
    recorded_at: int
    key_id: str  # From encryption wrapper
    dependencies: dict  # Empty for groups (self-contained)


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": True,
    "signer_type": "admin",  # Polymorphic: network or peer_shared
    "dependencies": [],  # Groups are self-contained
    "tables": ["groups"],
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: GroupInput) -> ProjectorResult:
    """Pure projection: dict -> result. No database access."""
    event_id = input_dict["event_id"]
    event_data: GroupEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    # Validate event type
    if event_data.get("type") != "group":
        return ProjectorResult(valid=False, reason="Invalid event type")

    # Check required fields
    name = event_data.get("name")
    if not name:
        return ProjectorResult(valid=False, reason="Missing group name")

    signed_by = event_data.get("signed_by")
    if not signed_by:
        return ProjectorResult(valid=False, reason="Missing signed_by")

    key_id = event_data.get("key_id")
    if not key_id:
        return ProjectorResult(valid=False, reason="Missing key_id")

    # Signature is verified by resolver (polymorphic: network or peer_shared)
    # If we got here, signature was valid

    # Build output row
    group_row = {
        "group_id": event_id,
        "name": name,
        "signed_by": signed_by,
        "created_at": event_data["created_at"],
        "key_id": key_id,
        "is_main": event_data.get("is_main", 0),
        "network_id": event_data.get("network_id", ""),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(valid=True, tables={"groups": [group_row]})


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    name: str = "Test Group",
    signed_by: str = "peer_shared_123",
    created_at: int = 1000000,
    key_id: str = "key_123",
    is_main: int = 0,
    network_id: str = "",
) -> dict:
    """Build event_data for testing."""
    result = {
        "type": "group",
        "name": name,
        "signed_by": signed_by,
        "created_at": created_at,
        "key_id": key_id,
        "is_main": is_main,
    }
    if network_id:
        result["network_id"] = network_id
    return result


def make_input(
    event_id: str = "grp_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    key_id: str = "key_123",
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "key_id": key_id,
        "dependencies": {},
    }


def make_network_signed_input(
    event_id: str = "grp_all_users",
    network_id: str = "net_123",
    name: str = "all_users",
    recorded_by: str = "peer_456",
) -> dict:
    """Build input for network-signed group (like all_users)."""
    return make_input(
        event_id=event_id,
        event_data=make_event_data(
            name=name,
            signed_by=network_id,  # Signed by network
            network_id=network_id,
            is_main=0,
        ),
        recorded_by=recorded_by,
    )
