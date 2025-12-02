"""Peer event type (local-only identity keypair).

Local-only: contains private key material, never synced.
Uses apply_result_device_wide since local_peers is device-wide.

SPEC/DEPS - declarative metadata for generic resolver
project() - pure function: input_dict -> ProjectorResult
create_pure() - pure function: deps -> CreateResult

API functions:
    create(t_ms, db) -> str
    project_event(peer_id, recorded_by, db) -> None
    get_private_key(peer_id, recorded_by, db) -> bytes
    get_public_key(peer_id, recorded_by, db) -> bytes
"""
from typing import Any, TypedDict
import json
import logging
import crypto
import store
from db import create_unsafe_db, create_safe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class PeerEventData(TypedDict):
    type: str
    public_key: str  # base64
    private_key: str  # base64
    created_at: int


class PeerCreateDeps(TypedDict):
    """Dependencies for peer creation."""
    private_key: bytes
    public_key: bytes


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,
    "signer_type": "none",  # Local-only, contains private key
    "dependencies": [],
    "tables": ["local_peers"],
    "device_wide": True,  # Use apply_result_device_wide
    "mark_valid": True,  # Mark in valid_events
    "generic_dispatch": True,
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    "key_material": {"type": "generated_keypair"},
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result. No database access."""
    from projection import ProjectorResult

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]

    public_key_b64 = event_data.get("public_key")
    private_key_b64 = event_data.get("private_key")
    created_at = event_data.get("created_at")

    if not all([public_key_b64, private_key_b64, created_at is not None]):
        return ProjectorResult(valid=False, reason="missing required fields")

    private_key = crypto.b64decode(private_key_b64)

    row = {
        "peer_id": event_id,
        "public_key": public_key_b64,
        "private_key": private_key,
        "created_at": created_at,
    }

    return ProjectorResult(valid=True, tables={"local_peers": [row]})


def create_pure(deps: PeerCreateDeps, t_ms: int):
    """Pure function to create a peer event.

    Peers are local-only identity keypairs.
    """
    from projection import CreateResult, BlobSpec, compute_event_id

    event_data = {
        'type': 'peer',
        'public_key': crypto.b64encode(deps['public_key']),
        'private_key': crypto.b64encode(deps['private_key']),
        'created_at': t_ms,
    }

    blob = json.dumps(event_data).encode()
    peer_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=peer_id, event_type='peer')],
        primary_id=peer_id,
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    public_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    private_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
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

def create(t_ms: int, db: Any) -> str:
    """Create a peer (local-only keypair).

    This is special: we can't use store_create_result() because we need
    the peer_id BEFORE we can create a recorded wrapper (peer sees itself).
    """
    unsafedb = create_unsafe_db(db)

    private_key, public_key = crypto.generate_keypair()
    deps = {'private_key': private_key, 'public_key': public_key}
    result = create_pure(deps, t_ms)

    # Store blob first to get peer_id
    blob_spec = result.blobs[0]
    peer_id = store.blob(blob_spec.blob, t_ms, return_dupes=True, unsafedb=unsafedb)

    # Then create recorded wrapper where peer sees itself
    from events.network import recorded
    recorded_id = recorded.create(peer_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project_event(recorded_id, db)

    log.info(f"peer.create() created peer_id={peer_id[:20]}...")
    return peer_id


# project_event() handled by generic dispatch (SPEC.generic_dispatch = True)


def get_private_key(peer_id: str, recorded_by: str, db: Any) -> bytes:
    """Get private key for a peer_id (only your own)."""
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access private key for peer {peer_id}")

    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT private_key FROM local_peers WHERE peer_id = ?", (peer_id,))
    if not row:
        raise ValueError(f"peer not found: {peer_id}")
    return row['private_key']


def get_public_key(peer_id: str, recorded_by: str, db: Any) -> bytes:
    """Get public key for a peer_id from local_peers (only your own)."""
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access local peer public key for peer {peer_id}")

    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT public_key FROM local_peers WHERE peer_id = ?", (peer_id,))
    if not row:
        raise ValueError(f"peer not found: {peer_id}")
    return crypto.b64decode(row['public_key'])
