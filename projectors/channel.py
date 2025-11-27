"""Channel projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class ChannelEventData(TypedDict):
    type: str
    name: str
    group_id: str
    signed_by: str
    created_at: int
    disappearing_time_ms: NotRequired[int]
    is_main: NotRequired[int]
    admin_grant: NotRequired[str]


class LinkedPeerDep(TypedDict):
    user_id: str
    peer_id: str


class AdminGrantDep(TypedDict):
    event_id: str
    user_id: str


class ChannelInput(TypedDict):
    event_id: str
    event_data: ChannelEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict  # {"signer_user": LinkedPeerDep | None, "admin_grant": AdminGrantDep | None}


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": True,
    "signer_type": "peer_shared",
    "dependencies": ["signer_user:linked_peer", "admin_grant:admin_grant?"],
    "tables": ["channels"],
}


# ============================================================================
# PROJECTOR
# ============================================================================

def project(input_dict: ChannelInput) -> ProjectorResult:
    """Pure projection: dict -> result. No database access."""
    event_id = input_dict["event_id"]
    event_data: ChannelEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict["dependencies"]

    # Get admin_grant from event
    admin_grant_id = event_data.get("admin_grant")

    if admin_grant_id:
        # Verify admin_grant authorizes the signer
        signer_user = deps.get("signer_user")
        if not signer_user:
            return ProjectorResult(blocked=True, missing_deps=["signer_user"])

        admin_grant = deps.get("admin_grant")
        if not admin_grant:
            return ProjectorResult(blocked=True, missing_deps=["admin_grant"])

        if admin_grant["user_id"] != signer_user["user_id"]:
            return ProjectorResult(
                valid=False,
                reason=f"admin_grant {admin_grant_id} does not authorize signer {signer_user['user_id']}"
            )

    channel_row = {
        "channel_id": event_id,
        "name": event_data.get("name"),
        "group_id": event_data.get("group_id"),
        "signed_by": event_data.get("signed_by"),
        "created_at": event_data.get("created_at"),
        "disappearing_time_ms": event_data.get("disappearing_time_ms", 0),
        "is_main": event_data.get("is_main", 0),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(valid=True, tables={"channels": [channel_row]})


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    name: str = "general",
    group_id: str = "grp_123",
    signed_by: str = "peer_123",
    created_at: int = 1000000,
    disappearing_time_ms: int = 0,
    is_main: int = 0,
    admin_grant: str | None = None,
) -> dict:
    """Build event_data for testing."""
    data = {
        "type": "channel",
        "name": name,
        "group_id": group_id,
        "signed_by": signed_by,
        "created_at": created_at,
        "disappearing_time_ms": disappearing_time_ms,
        "is_main": is_main,
    }
    if admin_grant:
        data["admin_grant"] = admin_grant
    return data


def make_input(
    event_id: str = "ch_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    signer_user: dict | None = None,
    admin_grant: dict | None = None,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {
            "signer_user": signer_user,
            "admin_grant": admin_grant,
        },
    }
