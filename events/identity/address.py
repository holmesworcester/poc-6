"""Address event type (peer's network address for direct communication).

SPEC/DEPS - declarative metadata for generic resolver
project() - pure function: input_dict -> ProjectorResult
create_pure() - pure function: deps -> CreateResult

API functions:
    create(peer_id, peer_shared_id, ip, port, t_ms, db) -> str
    project_event(address_id, recorded_by, recorded_at, db) -> str | None
"""
from typing import Any, TypedDict
import logging
import crypto
import store
from events.identity import peer
from db import create_safe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class AddressEventData(TypedDict):
    type: str
    peer_id: str  # peer_shared_id
    signed_by: str
    ip: str
    port: int
    created_at: int


class AddressCreateDeps(TypedDict):
    """Dependencies for address creation."""
    peer_shared_id: str
    private_key: bytes


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,
    "signer_type": "peer_shared",
    "dependencies": [],
    "tables": ["addresses"],
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

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    peer_shared_id = event_data.get("peer_id")
    ip = event_data.get("ip")
    port = event_data.get("port")
    created_at = event_data.get("created_at")

    if not all([peer_shared_id, ip, port is not None, created_at]):
        return ProjectorResult(valid=False, reason="Missing required fields")

    row = {
        "address_id": event_id,
        "peer_shared_id": peer_shared_id,
        "ip": ip,
        "port": port,
        "created_at": created_at,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(valid=True, tables={"addresses": [row]})


def create_pure(deps: AddressCreateDeps, ip: str, port: int, t_ms: int):
    """Pure function to create an address event."""
    from projection import CreateResult, BlobSpec, compute_event_id

    event_data = {
        'type': 'address',
        'peer_id': deps['peer_shared_id'],
        'signed_by': deps['peer_shared_id'],
        'ip': ip,
        'port': port,
        'created_at': t_ms,
    }

    signed_event = crypto.sign_event(event_data, deps['private_key'])
    blob = crypto.canonicalize_json(signed_event)
    address_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=address_id, event_type='address')],
        primary_id=address_id,
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    peer_id: str = "ps_123",
    signed_by: str = "ps_123",
    ip: str = "127.0.0.1",
    port: int = 6100,
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "address",
        "peer_id": peer_id,
        "signed_by": signed_by,
        "ip": ip,
        "port": port,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "addr_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
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

def create(peer_id: str, peer_shared_id: str, ip: str, port: int, t_ms: int, db: Any) -> str:
    """Create an address event for a peer."""
    from projection import store_create_result

    private_key = peer.get_private_key(peer_id, peer_id, db)

    deps = {
        'peer_shared_id': peer_shared_id,
        'private_key': private_key,
    }

    result = create_pure(deps, ip, port, t_ms)
    address_id = store_create_result(result, peer_id, t_ms, db)

    log.info(f"address.create() created address_id={address_id[:20]}...")
    return address_id


# project_event() handled by generic dispatch (SPEC.generic_dispatch = True)
