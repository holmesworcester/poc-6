"""Peer projector (local-only identity keypair).

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Local-only: contains private key material, never synced.
Uses apply_result_device_wide since local_peers is device-wide.
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

class PeerEventData(TypedDict):
    type: str
    public_key: str  # base64
    private_key: str  # base64
    created_at: int


class PeerInput(TypedDict):
    event_id: str  # peer_id
    event_data: PeerEventData
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
    "tables": ["local_peers"],  # Device-wide table
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    # Peer only needs generated keypair
    "key_material": {"type": "generated_keypair"},
}


# ============================================================================
# CREATE - pure function: deps -> CreateResult
# ============================================================================

class PeerCreateDeps(TypedDict):
    """Dependencies for peer creation."""
    private_key: bytes  # Generated keypair private
    public_key: bytes  # Generated keypair public


def create_pure(
    deps: PeerCreateDeps,
    t_ms: int,
) -> CreateResult:
    """Pure function to create a peer event.

    Peers are local-only identity keypairs. They store both public and
    private keys for signing operations.

    Args:
        deps: Resolved dependencies (includes generated keypair)
        t_ms: Timestamp

    Returns:
        CreateResult with peer blob
    """
    event_data = {
        'type': 'peer',
        'public_key': crypto.b64encode(deps['public_key']),
        'private_key': crypto.b64encode(deps['private_key']),
        'created_at': t_ms,
    }

    # Local-only: plain JSON, no signing/encryption
    blob = json.dumps(event_data).encode()
    peer_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=peer_id, event_type='peer')],
        primary_id=peer_id,
    )


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: PeerInput) -> ProjectorResult:
    """Pure projection: dict -> result.

    Outputs local_peers row. Use apply_result_device_wide() to write.
    """
    import crypto

    event_id = input_dict["event_id"]  # peer_id
    event_data: PeerEventData = input_dict["event_data"]

    public_key_b64 = event_data.get("public_key")
    private_key_b64 = event_data.get("private_key")
    created_at = event_data.get("created_at")

    if not all([public_key_b64, private_key_b64, created_at is not None]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Decode private key (stored as bytes in DB)
    private_key = crypto.b64decode(private_key_b64)

    # Output: local_peers row (device-wide table)
    # Note: public_key stays as base64 string, private_key is bytes
    row = {
        "peer_id": event_id,
        "public_key": public_key_b64,
        "private_key": private_key,
        "created_at": created_at,
    }

    return ProjectorResult(
        valid=True,
        tables={"local_peers": [row]},
    )


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    public_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 bytes base64
    private_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 bytes base64
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "peer",
        "public_key": public_key,
        "private_key": private_key,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "peer_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_123",  # Usually same as event_id for local peers
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
