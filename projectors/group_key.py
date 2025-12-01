"""Group key projector (subjective symmetric keys for network/group content encryption).

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Note: group_key events are DETERMINISTIC (no timestamp in blob).
created_at comes from recorded_at parameter.
Uses apply_result since group_keys is subjective (has recorded_by).
"""

from typing import TypedDict
from projectors import ProjectorResult
import logging

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
