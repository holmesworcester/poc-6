"""Group prekey shared projector.

Shareable public prekey for group key wrapping.
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


class GroupPrekeySharedEventData(TypedDict):
    type: str
    group_prekey_id: str
    peer_id: str  # peer_shared_id
    public_key: str  # base64 encoded
    signed_by: str
    created_at: int
    group_id: NotRequired[str]  # For mode=user invites
    key_id: NotRequired[str]    # For mode=user invites
    user_id: NotRequired[str]   # For mode=link invites


class GroupPrekeySharedInput(TypedDict):
    event_id: str
    event_data: GroupPrekeySharedEventData
    recorded_by: str
    recorded_at: int


SPEC = {
    "encrypted": False,
    "signer_type": "peer_shared",
    "dependencies": [],
    "tables": ["group_prekeys_shared"],
}


def project(input_dict: GroupPrekeySharedInput) -> ProjectorResult:
    """Pure projection: dict -> result."""
    import crypto

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]

    peer_id = event_data.get("peer_id")
    public_key_b64 = event_data.get("public_key")
    created_at = event_data.get("created_at")

    if not all([peer_id, public_key_b64, created_at]):
        return ProjectorResult(valid=False, reason="Missing required fields")

    public_key = crypto.b64decode(public_key_b64)

    row = {
        "group_prekey_shared_id": event_id,
        "peer_id": peer_id,
        "public_key": public_key,
        "created_at": created_at,
        "recorded_by": recorded_by,
    }

    return ProjectorResult(valid=True, tables={"group_prekeys_shared": [row]})


# Test builders
def make_event_data(
    group_prekey_id: str = "gpk_123",
    peer_id: str = "ps_123",
    public_key: str = "cHVibGljX2tleV9ieXRlcw==",
    signed_by: str = "ps_123",
    created_at: int = 1000000,
    group_id: str | None = "grp_123",
    key_id: str | None = "key_123",
    user_id: str | None = None,
) -> dict:
    data = {
        "type": "group_prekey_shared",
        "group_prekey_id": group_prekey_id,
        "peer_id": peer_id,
        "public_key": public_key,
        "signed_by": signed_by,
        "created_at": created_at,
    }
    if group_id:
        data["group_id"] = group_id
    if key_id:
        data["key_id"] = key_id
    if user_id:
        data["user_id"] = user_id
    return data


def make_input(
    event_id: str = "gpks_123",
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
