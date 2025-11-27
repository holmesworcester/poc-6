"""Group member projector.

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

class GroupMemberEventData(TypedDict):
    type: str
    group_id: str
    user_id: str
    added_by: str
    signed_by: str
    created_at: int
    admin_grant: NotRequired[str]


class EventDep(TypedDict):
    event_id: str
    event_data: dict


class LinkedPeerDep(TypedDict):
    user_id: str
    peer_id: str


class AdminGrantDep(TypedDict):
    event_id: str
    user_id: str


class GroupMemberInput(TypedDict):
    event_id: str
    event_data: GroupMemberEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict  # {"group": EventDep, "user": EventDep, "adder_user": LinkedPeerDep | None, "admin_grant": AdminGrantDep | None}


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": True,
    "signer_type": "peer_shared",
    "signer_field": "added_by",  # group_member uses added_by instead of signed_by
    "dependencies": ["group:event", "user:event", "adder_user:linked_peer", "admin_grant:admin_grant?"],
    "tables": ["group_members"],
}


# ============================================================================
# PROJECTOR
# ============================================================================

def project(input_dict: GroupMemberInput) -> ProjectorResult:
    """Pure projection: dict -> result. No database access."""
    event_id = input_dict["event_id"]
    event_data: GroupMemberEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict["dependencies"]

    # Check group exists
    if not deps.get("group"):
        return ProjectorResult(blocked=True, missing_deps=["group"])

    # Check user exists
    if not deps.get("user"):
        return ProjectorResult(blocked=True, missing_deps=["user"])

    # Authorization check
    admin_grant_id = event_data.get("admin_grant")
    added_by = event_data.get("added_by")
    group_data = deps["group"]["event_data"]

    if admin_grant_id:
        # Explicit admin_grant - verify it authorizes the adder
        adder_user = deps.get("adder_user")
        if not adder_user:
            return ProjectorResult(blocked=True, missing_deps=["adder_user"])

        admin_grant = deps.get("admin_grant")
        if not admin_grant:
            return ProjectorResult(blocked=True, missing_deps=["admin_grant"])

        if admin_grant["user_id"] != adder_user["user_id"]:
            return ProjectorResult(
                valid=False,
                reason=f"admin_grant does not authorize adder {adder_user['user_id']}"
            )
    else:
        # Legacy: adder must be group creator
        if added_by and added_by != group_data.get("signed_by"):
            return ProjectorResult(
                valid=False,
                reason=f"adder {added_by} is not group creator and no admin_grant provided"
            )

    member_row = {
        "member_id": event_id,
        "group_id": event_data["group_id"],
        "user_id": event_data["user_id"],
        "added_by": added_by,
        "created_at": event_data["created_at"],
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(valid=True, tables={"group_members": [member_row]})


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    group_id: str = "grp_123",
    user_id: str = "user_123",
    added_by: str = "peer_123",
    created_at: int = 1000000,
    admin_grant: str | None = None,
) -> dict:
    """Build event_data for testing."""
    data = {
        "type": "group_member",
        "group_id": group_id,
        "user_id": user_id,
        "added_by": added_by,
        "signed_by": added_by,
        "created_at": created_at,
    }
    if admin_grant:
        data["admin_grant"] = admin_grant
    return data


def make_group_dep(
    group_id: str = "grp_123",
    signed_by: str = "peer_123",
) -> dict:
    """Build group dependency for testing."""
    return {
        "event_id": group_id,
        "event_data": {"type": "group", "signed_by": signed_by},
    }


def make_user_dep(user_id: str = "user_123") -> dict:
    """Build user dependency for testing."""
    return {
        "event_id": user_id,
        "event_data": {"type": "user", "name": "Test User"},
    }


def make_input(
    event_id: str = "member_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    group: dict | None = None,
    user: dict | None = None,
    adder_user: dict | None = None,
    admin_grant: dict | None = None,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {
            "group": group if group is not None else make_group_dep(),
            "user": user if user is not None else make_user_dep(),
            "adder_user": adder_user,
            "admin_grant": admin_grant,
        },
    }
