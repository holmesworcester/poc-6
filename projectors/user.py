"""User projector.

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

class UserEventData(TypedDict):
    type: str
    invite_id: str
    signed_by: str
    name: str
    user_pubkey: str
    created_at: int
    network_id: NotRequired[str]


class InviteEventData(TypedDict):
    type: str
    invite_pubkey: str
    group_id: NotRequired[str]
    network_id: NotRequired[str]
    inviter_peer_shared_id: NotRequired[str]


class InviteDep(TypedDict):
    event_id: str
    event_data: InviteEventData


class UserInput(TypedDict):
    event_id: str
    event_data: UserEventData
    recorded_by: str
    recorded_at: int
    signature_valid: bool
    dependencies: dict  # {"invite": InviteDep | None}


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # User events are plaintext, not encrypted
    "signer_type": "invite",  # Signed by invite key
    "dependencies": ["invite:invite"],
    "tables": ["users", "valid_events", "group_members"],
}


# ============================================================================
# PROJECTOR
# ============================================================================

def project(input_dict: UserInput) -> ProjectorResult:
    """Pure projection: dict -> result. No database access."""
    event_id = input_dict["event_id"]
    event_data: UserEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict["dependencies"]

    # Validate structure
    invite_id = event_data.get("invite_id")
    signed_by = event_data.get("signed_by")

    if not invite_id:
        return ProjectorResult(valid=False, reason="Missing invite_id")

    if signed_by != invite_id:
        return ProjectorResult(valid=False, reason=f"signed_by doesn't match invite_id")

    # Check invite dependency
    invite = deps.get("invite")
    if not invite:
        return ProjectorResult(blocked=True, missing_deps=["invite"])

    # Check signature
    if not input_dict.get("signature_valid"):
        return ProjectorResult(valid=False, reason="Signature verification failed")

    # Get network_id from event or invite
    network_id = event_data.get("network_id")
    if not network_id:
        network_id = invite["event_data"].get("network_id")

    # Build output tables
    users_row = {
        "user_id": event_id,
        "name": event_data.get("name", ""),
        "network_id": network_id,
        "created_at": event_data.get("created_at", 0),
        "user_pubkey": event_data.get("user_pubkey", ""),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    valid_events_row = {
        "event_id": event_id,
        "recorded_by": recorded_by,
    }

    tables = {
        "users": [users_row],
        "valid_events": [valid_events_row],
    }

    # Auto-add to invite's group if present
    group_id = invite["event_data"].get("group_id")
    if group_id:
        inviter = invite["event_data"].get("inviter_peer_shared_id", signed_by)
        tables["group_members"] = [{
            "member_id": event_id,
            "group_id": group_id,
            "user_id": event_id,
            "added_by": inviter,
            "created_at": event_data.get("created_at", 0),
            "recorded_by": recorded_by,
            "recorded_at": recorded_at,
        }]

    return ProjectorResult(valid=True, tables=tables)


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    invite_id: str = "inv_123",
    name: str = "Test User",
    user_pubkey: str = "pubkey_123",
    created_at: int = 1000000,
    network_id: str | None = None,
) -> dict:
    """Build event_data for testing."""
    data = {
        "type": "user",
        "invite_id": invite_id,
        "signed_by": invite_id,  # User events signed by invite
        "name": name,
        "user_pubkey": user_pubkey,
        "created_at": created_at,
    }
    if network_id:
        data["network_id"] = network_id
    return data


def make_invite_dep(
    invite_id: str = "inv_123",
    group_id: str = "grp_123",
    network_id: str | None = "net_123",
    inviter_peer_shared_id: str = "peer_inviter",
) -> dict:
    """Build invite dependency for testing."""
    return {
        "event_id": invite_id,
        "event_data": {
            "type": "invite",
            "invite_pubkey": "pubkey_xyz",
            "group_id": group_id,
            "network_id": network_id,
            "inviter_peer_shared_id": inviter_peer_shared_id,
        },
    }


def make_input(
    event_id: str = "user_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    signature_valid: bool = True,
    invite: dict | None = None,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "signature_valid": signature_valid,
        "dependencies": {
            "invite": invite if invite is not None else make_invite_dep(),
        },
    }
