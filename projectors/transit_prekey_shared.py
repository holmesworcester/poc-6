"""Transit prekey shared projector.

Shareable public transit prekey for sync routing.
"""

from typing import TypedDict
from projectors import ProjectorResult, CreateResult, BlobSpec, compute_event_id
import logging
import crypto

log = logging.getLogger(__name__)


class TransitPrekeySharedEventData(TypedDict):
    type: str
    transit_prekey_id: str
    peer_id: str  # peer_shared_id
    public_key: str  # base64 encoded
    signed_by: str
    created_at: int


class TransitPrekeySharedInput(TypedDict):
    event_id: str
    event_data: TransitPrekeySharedEventData
    recorded_by: str
    recorded_at: int


SPEC = {
    "encrypted": False,
    "signer_type": "peer_shared",
    "dependencies": [],
    "tables": ["transit_prekeys_shared"],
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    # Private key for signing
    "private_key": {"type": "local_peer_key"},
    # Public key from local transit_prekey (passed by caller)
}


# ============================================================================
# CREATE - pure function: deps -> CreateResult
# ============================================================================

class TransitPrekeySharedCreateDeps(TypedDict):
    """Dependencies for transit_prekey_shared creation."""
    peer_shared_id: str
    private_key: bytes
    prekey_id: str  # Local transit_prekey ID
    public_key_b64: str  # Public key from local prekey (base64)


def create_pure(
    deps: TransitPrekeySharedCreateDeps,
    t_ms: int,
) -> CreateResult:
    """Pure function to create a transit_prekey_shared event.

    Transit prekey shared events publish a transit prekey's public key
    so other peers can send sealed sync requests.

    Args:
        deps: Resolved dependencies
        t_ms: Timestamp

    Returns:
        CreateResult with prekey_shared blob
    """
    event_data = {
        'type': 'transit_prekey_shared',
        'transit_prekey_id': deps['prekey_id'],
        'peer_id': deps['peer_shared_id'],
        'public_key': deps['public_key_b64'],
        'signed_by': deps['peer_shared_id'],
        'created_at': t_ms,
    }

    # Sign the event
    signed_event = crypto.sign_event(event_data, deps['private_key'])

    # Canonicalize (plaintext, no encryption)
    blob = crypto.canonicalize_json(signed_event)
    prekey_shared_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=prekey_shared_id, event_type='transit_prekey_shared')],
        primary_id=prekey_shared_id,
    )


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: TransitPrekeySharedInput) -> ProjectorResult:
    """Pure projection: dict -> result."""
    import crypto

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]

    transit_prekey_id = event_data.get("transit_prekey_id")
    peer_id = event_data.get("peer_id")
    public_key_b64 = event_data.get("public_key")
    created_at = event_data.get("created_at")

    if not all([transit_prekey_id, peer_id, public_key_b64, created_at]):
        return ProjectorResult(valid=False, reason="Missing required fields")

    public_key = crypto.b64decode(public_key_b64)

    row = {
        "transit_prekey_shared_id": event_id,
        "transit_prekey_id": transit_prekey_id,
        "peer_id": peer_id,
        "public_key": public_key,
        "created_at": created_at,
        "recorded_by": recorded_by,
    }

    return ProjectorResult(valid=True, tables={"transit_prekeys_shared": [row]})


# Test builders
def make_event_data(
    transit_prekey_id: str = "tpk_123",
    peer_id: str = "ps_123",
    public_key: str = "cHVibGljX2tleV9ieXRlcw==",
    signed_by: str = "ps_123",
    created_at: int = 1000000,
) -> dict:
    return {
        "type": "transit_prekey_shared",
        "transit_prekey_id": transit_prekey_id,
        "peer_id": peer_id,
        "public_key": public_key,
        "signed_by": signed_by,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "tpks_123",
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
