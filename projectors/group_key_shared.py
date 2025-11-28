"""Group key shared projector.

Pure computation for group_key_shared events.
Note: Unwrapping and signature verification are handled by the event module.
This projector only handles the pure computation after those checks pass.
"""

from typing import TypedDict
from projectors.project import ProjectorResult
import logging

log = logging.getLogger(__name__)


class GroupKeySharedEventData(TypedDict):
    type: str
    key_id: str  # Original key_id from sender
    symmetric_key: str  # base64 encoded key material
    signed_by: str
    created_at: int


class GroupKeySharedInput(TypedDict):
    event_id: str
    event_data: GroupKeySharedEventData
    recorded_by: str
    recorded_at: int


# Note: This projector is NOT used via resolve() because group_key_shared
# has special unwrapping requirements (unwrap_event, not unwrap).
# The event module handles unwrapping and calls this projector directly.
SPEC = {
    "encrypted": True,  # Wrapped to recipient prekey
    "signer_type": "peer_shared",
    "dependencies": [],
    "tables": ["group_keys_shared"],
}


def project(input_dict: GroupKeySharedInput) -> ProjectorResult:
    """Pure projection: dict -> result with derived group_key event.

    Assumes event_data has already been unwrapped and signature verified.
    Returns tables to write and derived events to create.
    """
    import crypto

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    original_key_id = event_data.get("key_id")
    symmetric_key_b64 = event_data.get("symmetric_key")
    signed_by = event_data.get("signed_by")
    created_at = event_data.get("created_at")

    if not all([original_key_id, symmetric_key_b64, signed_by, created_at]):
        return ProjectorResult(valid=False, reason="Missing required fields")

    # Decode key material
    symmetric_key = crypto.b64decode(symmetric_key_b64)

    # Compute deterministic key_id from key material
    # This must match the original key_id (same key material = same hash)
    computed_key_id = crypto.hash_group_key_material(symmetric_key)

    # Security check: key_id must match
    # A mismatch indicates corruption or malicious sender providing wrong material
    if computed_key_id != original_key_id:
        log.error(f"group_key_shared key_id mismatch! computed={computed_key_id[:20]}... vs original={original_key_id[:20]}...")
        return ProjectorResult(valid=False, reason="key_id mismatch - possible tampering")

    # Output: group_keys_shared row
    gks_row = {
        "key_shared_id": event_id,
        "original_key_id": computed_key_id,  # Use computed (deterministic) key_id
        "signed_by": signed_by,
        "created_at": created_at,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    # Output: derived group_key event to create
    # The framework will call group_key.create_with_material() in apply_result()
    derived_group_key = {
        "type": "group_key",
        "symmetric_key": symmetric_key,  # Raw bytes
        "created_at": created_at,
    }

    return ProjectorResult(
        valid=True,
        tables={"group_keys_shared": [gks_row]},
        derived_events=[derived_group_key],
    )


# Test builders
def make_event_data(
    key_id: str = "key_123",
    symmetric_key: str = "c3ltbWV0cmljX2tleQ==",  # base64 of "symmetric_key"
    signed_by: str = "ps_123",
    created_at: int = 1000000,
) -> dict:
    return {
        "type": "group_key_shared",
        "key_id": key_id,
        "symmetric_key": symmetric_key,
        "signed_by": signed_by,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "gks_123",
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
