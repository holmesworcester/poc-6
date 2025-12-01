"""Group key shared projector.

Pure computation for group_key_shared events.
Note: Unwrapping and signature verification are handled by the event module.
This projector only handles the pure computation after those checks pass.
"""

from typing import TypedDict
from projectors.project import ProjectorResult, CreateResult, BlobSpec, compute_event_id
import logging
import crypto

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


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    # Private key for signing
    "private_key": {"type": "local_peer_key"},
    # peer_shared_id from peer_self
    "peer_self": {"table": "peer_self", "key_field": "peer_id", "fields": ["peer_shared_id"]},
    # Symmetric key to share (from group_key)
    "key": {"table": "group_keys", "key_field": "key_id", "fields": ["symmetric_key"]},
    # Recipient prekey for sealing (provided by caller - can be peer or invite prekey)
}


# ============================================================================
# CREATE - pure function: deps -> CreateResult
# ============================================================================

class GroupKeySharedCreateDeps(TypedDict):
    """Dependencies for group_key_shared creation."""
    peer_shared_id: str
    private_key: bytes
    key_id: str
    symmetric_key: bytes  # The key material to share
    recipient_prekey: dict  # {id: bytes, public_key: bytes, type: 'asymmetric'}


def create_pure(
    deps: GroupKeySharedCreateDeps,
    t_ms: int,
) -> CreateResult:
    """Pure function to create a group_key_shared event.

    Group_key_shared events seal a symmetric key to a recipient's prekey
    using asymmetric encryption. The recipient can decrypt with their
    private key to recover the symmetric key.

    Args:
        deps: Resolved dependencies including recipient prekey
        t_ms: Timestamp

    Returns:
        CreateResult with sealed blob
    """
    # Build inner event
    inner_event_data = {
        'type': 'group_key_shared',
        'key_id': deps['key_id'],  # Reference to the key being shared
        'symmetric_key': crypto.b64encode(deps['symmetric_key']),  # The actual key material
        'signed_by': deps['peer_shared_id'],
        'created_at': t_ms,
    }

    # Sign the inner event
    signed_inner_event = crypto.sign_event(inner_event_data, deps['private_key'])

    # Wrap (asymmetric encrypt) to recipient's prekey
    canonical = crypto.canonicalize_json(signed_inner_event)
    wrapped_blob = crypto.wrap(canonical, deps['recipient_prekey'], None)
    key_shared_id = compute_event_id(wrapped_blob)

    return CreateResult(
        blobs=[BlobSpec(blob=wrapped_blob, event_id=key_shared_id, event_type='group_key_shared')],
        primary_id=key_shared_id,
    )


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

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
