"""Group key projector (subjective symmetric keys for network/group content encryption).

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Note: group_key events are DETERMINISTIC (no timestamp in blob).
created_at comes from recorded_at parameter.
Uses apply_result since group_keys is subjective (has recorded_by).
"""

from typing import TypedDict
from projectors import ProjectorResult, CreateResult, BlobSpec, compute_event_id
import logging
import json
import crypto

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class GroupKeyEventData(TypedDict):
    type: str
    key: str  # base64 symmetric key


class GroupKeyInput(TypedDict):
    event_id: str  # key_id
    event_data: GroupKeyEventData
    recorded_by: str
    recorded_at: int  # Used as created_at since blob has no timestamp
    dependencies: dict


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plain JSON (local-only)
    "signer_type": "none",  # Not signed (local-only, deterministic)
    "dependencies": [],
    "tables": ["group_keys"],  # Subjective table (has recorded_by)
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    # Group keys only need generated secret
    "key_material": {"type": "generated_secret"},
}


# ============================================================================
# CREATE - pure function: deps -> CreateResult
# ============================================================================

class GroupKeyCreateDeps(TypedDict):
    """Dependencies for group_key creation."""
    key_material: bytes  # Generated symmetric key (32 bytes)


def create_pure(
    deps: GroupKeyCreateDeps,
) -> CreateResult:
    """Pure function to create a group_key event.

    Group keys are DETERMINISTIC - same key material produces same key_id.
    No timestamp in blob ensures identical content-addressed IDs across peers.

    Args:
        deps: Resolved dependencies (includes generated key material)

    Returns:
        CreateResult with key blob
    """
    # DETERMINISTIC blob - only type and key, no timestamp
    event_data = {
        'type': 'group_key',
        'key': crypto.b64encode(deps['key_material']),
    }

    # Use sort_keys=True for canonical ordering
    blob = json.dumps(event_data, sort_keys=True).encode()
    key_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=key_id, event_type='group_key')],
        primary_id=key_id,
    )


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: GroupKeyInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Outputs group_keys row. Use apply_result() since group_keys is subjective.
    Note: created_at comes from recorded_at (deterministic blobs have no timestamp).
    """
    import crypto

    event_id = input_dict["event_id"]  # key_id
    event_data: GroupKeyEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    key_b64 = event_data.get("key")

    if not key_b64:
        return ProjectorResult(valid=False, reason="missing required field: key")

    # Decode key (stored as bytes in DB)
    key = crypto.b64decode(key_b64)

    # Output: group_keys row (subjective table)
    # Note: created_at comes from recorded_at since blob has no timestamp
    row = {
        "key_id": event_id,
        "key": key,
        "created_at": recorded_at,
        "recorded_by": recorded_by,
    }

    return ProjectorResult(
        valid=True,
        tables={"group_keys": [row]},
    )


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 bytes base64
) -> dict:
    """Build event_data for testing.

    Note: group_key events are deterministic - only type and key, no timestamp.
    """
    return {
        "type": "group_key",
        "key": key,
    }


def make_input(
    event_id: str = "key_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_123",
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
