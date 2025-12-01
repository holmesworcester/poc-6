"""Transit key projector (device-wide symmetric keys for sync routing).

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Local-only: transit keys are sync protocol infrastructure.
Uses apply_result_device_wide since transit_keys is device-wide.
"""

from typing import TypedDict
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class TransitKeyEventData(TypedDict):
    type: str
    key: str  # base64 symmetric key
    signed_by: str  # owner peer_id
    created_at: int


class TransitKeyInput(TypedDict):
    event_id: str  # key_id
    event_data: TransitKeyEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plain JSON (local-only)
    "signer_type": "none",  # Not signed (local-only)
    "dependencies": [],
    "tables": ["transit_keys"],  # Device-wide table
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: TransitKeyInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Outputs transit_keys row. Use apply_result_device_wide() to write.
    """
    import crypto

    event_id = input_dict["event_id"]  # key_id
    event_data: TransitKeyEventData = input_dict["event_data"]

    key_b64 = event_data.get("key")
    signed_by = event_data.get("signed_by")  # owner peer_id
    created_at = event_data.get("created_at")

    if not all([key_b64, signed_by, created_at is not None]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Decode key (stored as bytes in DB)
    key = crypto.b64decode(key_b64)

    # Output: transit_keys row (device-wide table)
    row = {
        "key_id": event_id,
        "key": key,
        "owner_peer_id": signed_by,
        "created_at": created_at,
    }

    return ProjectorResult(
        valid=True,
        tables={"transit_keys": [row]},
    )


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 bytes base64
    signed_by: str = "peer_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "transit_key",
        "key": key,
        "signed_by": signed_by,
        "created_at": created_at,
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
