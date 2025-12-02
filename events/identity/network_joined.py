"""Network joined event type - marks successful bootstrap with inviter.

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    create(peer_id, peer_shared_id, inviter_peer_shared_id, t_ms, db) -> str
    project_event(event_id, recorded_by, recorded_at, db) -> str | None
"""
from typing import Any, TypedDict
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

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


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result."""
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
    recorded_by: str = "peer_123",  # Must match peer_id for projection to occur
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
    """Create network_joined event after successful bootstrap.

    Args:
        peer_id: Joiner's peer_id
        peer_shared_id: Joiner's peer_shared_id
        inviter_peer_shared_id: Inviter's peer_shared_id
        t_ms: Timestamp
        db: Database connection

    Returns:
        network_joined event ID
    """
    from events.identity import peer

    # Create event data
    event_data = {
        'type': 'network_joined',
        'peer_id': peer_id,
        'signed_by': peer_shared_id,
        'inviter_peer_shared_id': inviter_peer_shared_id,
        'created_at': t_ms
    }

    # Get joiner's private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)
    if not private_key:
        log.error(f"network_joined.create() no private key for peer {peer_id[:20]}...")
        raise ValueError(f"No private key for peer {peer_id}")

    # Sign the event
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext
    canonical = crypto.canonicalize_json(signed_event)
    network_joined_id = store.event(canonical, peer_id, t_ms, db)

    log.info(f"network_joined.create() created event {network_joined_id[:20]}... for peer {peer_id[:20]}...")

    return network_joined_id


def project_event(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project network_joined event into network_joiners table."""
    from projectors import resolve, apply_result
    from projectors import network_joined as nj_projector

    input_dict = resolve("network_joined", event_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = nj_projector.project(input_dict)

    if not result.valid:
        log.warning(f"network_joined.project() failed: {result.reason}")
        return None

    apply_result(result, recorded_by, recorded_at, db)
    return event_id
