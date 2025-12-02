"""Network joined event type - marks successful bootstrap with inviter.

SPEC/DEPS - declarative metadata for generic resolver
project() - pure function: input_dict -> ProjectorResult
create_pure() - pure function: deps -> CreateResult

API functions:
    create(peer_id, peer_shared_id, inviter_peer_shared_id, t_ms, db) -> str
    project_event(event_id, recorded_by, recorded_at, db) -> str | None
"""
from typing import Any, TypedDict
import logging
import crypto
import store

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class NetworkJoinedEventData(TypedDict):
    type: str
    peer_id: str
    signed_by: str
    inviter_peer_shared_id: str
    created_at: int


class NetworkJoinedCreateDeps(TypedDict):
    """Dependencies for network_joined creation."""
    peer_id: str
    peer_shared_id: str
    private_key: bytes
    inviter_peer_shared_id: str


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,
    "signer_type": "none",  # Local-only
    "dependencies": [],
    "tables": ["network_joiners"],
    "generic_dispatch": True,
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    "private_key": {"type": "local_peer_key"},
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result. No database access."""
    from projection import ProjectorResult

    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]

    peer_id = event_data.get("peer_id")
    inviter_peer_shared_id = event_data.get("inviter_peer_shared_id")

    if not peer_id or not inviter_peer_shared_id:
        return ProjectorResult(valid=False, reason="Missing peer_id or inviter_peer_shared_id")

    # Only project our own network_joined event
    if recorded_by != peer_id:
        return ProjectorResult(valid=True, tables={})

    row = {
        "peer_id": peer_id,
        "recorded_by": recorded_by,
    }

    return ProjectorResult(valid=True, tables={"network_joiners": [row]})


def create_pure(deps: NetworkJoinedCreateDeps, t_ms: int):
    """Pure function to create a network_joined event."""
    from projection import CreateResult, BlobSpec, compute_event_id

    event_data = {
        'type': 'network_joined',
        'peer_id': deps['peer_id'],
        'signed_by': deps['peer_shared_id'],
        'inviter_peer_shared_id': deps['inviter_peer_shared_id'],
        'created_at': t_ms,
    }

    signed_event = crypto.sign_event(event_data, deps['private_key'])
    blob = crypto.canonicalize_json(signed_event)
    network_joined_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=network_joined_id, event_type='network_joined')],
        primary_id=network_joined_id,
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    peer_id: str = "peer_123",
    signed_by: str = "peer_shared_123",
    inviter_peer_shared_id: str = "ps_inviter",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "network_joined",
        "peer_id": peer_id,
        "signed_by": signed_by,
        "inviter_peer_shared_id": inviter_peer_shared_id,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "nj_123",
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


# ============================================================================
# API FUNCTIONS
# ============================================================================

def create(peer_id: str, peer_shared_id: str, inviter_peer_shared_id: str,
           t_ms: int, db: Any) -> str:
    """Create network_joined event after successful bootstrap."""
    from projection import store_create_result
    from events.identity import peer

    private_key = peer.get_private_key(peer_id, peer_id, db)
    if not private_key:
        raise ValueError(f"No private key for peer {peer_id}")

    deps = {
        'peer_id': peer_id,
        'peer_shared_id': peer_shared_id,
        'private_key': private_key,
        'inviter_peer_shared_id': inviter_peer_shared_id,
    }

    result = create_pure(deps, t_ms)
    network_joined_id = store_create_result(result, peer_id, t_ms, db)

    log.info(f"network_joined.create() created event {network_joined_id[:20]}...")
    return network_joined_id


def project_event(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project network_joined event. Uses generic resolver."""
    from projection import resolve, apply_result

    input_dict = resolve("network_joined", event_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = project(input_dict)

    if not result.valid:
        log.warning(f"network_joined.project_event() failed: {result.reason}")
        return None

    apply_result(result, recorded_by, recorded_at, db)
    return event_id
