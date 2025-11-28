"""Network projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Network events are self-signed (root of trust). The signature is verified
using the network_pubkey contained within the event itself.
"""

from typing import TypedDict
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class NetworkEventData(TypedDict):
    type: str
    signed_by: str  # Always 'SELF' for networks
    network_pubkey: str  # Base64-encoded public key
    created_at: int


class NetworkInput(TypedDict):
    event_id: str
    event_data: NetworkEventData
    recorded_by: str
    recorded_at: int
    signature_valid: bool
    dependencies: dict  # Empty for networks (self-signed, no deps)


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Network events are plaintext
    "signer_type": "self",  # Self-signed (root of trust)
    "dependencies": [],  # No dependencies
    "tables": ["networks"],
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: NetworkInput) -> ProjectorResult:
    """Pure projection: dict -> result. No database access."""
    event_id = input_dict["event_id"]
    event_data: NetworkEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    # Validate signed_by is 'SELF'
    signed_by = event_data.get("signed_by")
    if signed_by != "SELF":
        return ProjectorResult(valid=False, reason=f"Expected signed_by='SELF', got {signed_by}")

    # Check signature
    if not input_dict.get("signature_valid"):
        return ProjectorResult(valid=False, reason="Self-signature verification failed")

    # Check required field
    network_pubkey = event_data.get("network_pubkey")
    if not network_pubkey:
        return ProjectorResult(valid=False, reason="Missing network_pubkey in event")

    # Build output row
    network_row = {
        "network_id": event_id,
        "creator_user_id": "",  # Set later by admin.project()
        "network_pubkey": network_pubkey,
        "signed_by": signed_by,
        "created_at": event_data["created_at"],
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(valid=True, tables={"networks": [network_row]})


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    network_pubkey: str = "pubkey_abc123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "network",
        "signed_by": "SELF",
        "network_pubkey": network_pubkey,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "net_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    signature_valid: bool = True,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "signature_valid": signature_valid,
        "dependencies": {},
    }
