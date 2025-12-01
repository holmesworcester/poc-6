"""Transit prekey projector (device-wide asymmetric prekey for sync routing).

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Transit prekeys contain both public and private keys (local-only).
TTL is calculated from created_at + TRANSIT_PREKEY_TTL_MS.
Uses apply_result_device_wide since transit_prekeys is device-wide.
"""

from typing import TypedDict
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)

# Transit prekeys expire after 7 days (in milliseconds)
TRANSIT_PREKEY_TTL_MS = 7 * 24 * 60 * 60 * 1000


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class TransitPrekeyEventData(TypedDict):
    type: str
    public_key: str  # base64
    private_key: str  # base64
    signed_by: str  # owner peer_id
    created_at: int


class TransitPrekeyInput(TypedDict):
    event_id: str  # prekey_id
    event_data: TransitPrekeyEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plain JSON (local-only)
    "signer_type": "none",  # Not signed (local-only, contains private key)
    "dependencies": [],
    "tables": ["transit_prekeys"],  # Device-wide table
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: TransitPrekeyInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Outputs transit_prekeys row. Use apply_result_device_wide().
    Calculates TTL from created_at + TRANSIT_PREKEY_TTL_MS.
    """
    import crypto

    event_id = input_dict["event_id"]  # prekey_id
    event_data: TransitPrekeyEventData = input_dict["event_data"]

    public_key_b64 = event_data.get("public_key")
    private_key_b64 = event_data.get("private_key")
    owner_peer_id = event_data.get("signed_by")
    created_at = event_data.get("created_at")

    if not all([public_key_b64, private_key_b64, owner_peer_id, created_at is not None]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Decode keys (stored as bytes in DB)
    public_key = crypto.b64decode(public_key_b64)
    private_key = crypto.b64decode(private_key_b64)

    # Calculate TTL: absolute time when this prekey expires
    ttl_ms = created_at + TRANSIT_PREKEY_TTL_MS

    # Output: transit_prekeys row (device-wide table)
    row = {
        "transit_prekey_id": event_id,
        "owner_peer_id": owner_peer_id,
        "public_key": public_key,
        "private_key": private_key,
        "created_at": created_at,
        "ttl_ms": ttl_ms,
    }

    return ProjectorResult(
        valid=True,
        tables={"transit_prekeys": [row]},
    )


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    public_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 bytes base64
    private_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 bytes base64
    signed_by: str = "peer_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "transit_prekey",
        "public_key": public_key,
        "private_key": private_key,
        "signed_by": signed_by,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "prekey_123",
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
