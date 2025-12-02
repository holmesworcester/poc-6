"""Bootstrap complete event type - marks when a joiner receives first sync request.

Local-only event marking bootstrap completion.

SPEC - declarative metadata for generic resolver
project() - pure function: input_dict -> ProjectorResult

API functions:
    create(peer_id, t_ms, db) -> str
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

class BootstrapCompleteEventData(TypedDict):
    type: str
    peer_id: str
    created_at: int


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,
    "signer_type": "none",  # Local-only, no signature verification
    "dependencies": [],
    "tables": ["bootstrap_completers"],
    "generic_dispatch": True,  # Use generic project_event() from projection.py
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

    if not peer_id:
        return ProjectorResult(valid=False, reason="Missing peer_id")

    # Only project our own bootstrap_complete event
    if recorded_by != peer_id:
        return ProjectorResult(valid=True, tables={})

    row = {
        "peer_id": peer_id,
        "recorded_by": recorded_by,
    }

    return ProjectorResult(valid=True, tables={"bootstrap_completers": [row]})


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(peer_id: str = "peer_123", created_at: int = 1000000) -> dict:
    """Build event_data for testing."""
    return {
        "type": "bootstrap_complete",
        "peer_id": peer_id,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "bc_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_123",
    recorded_at: int = 1000001,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(peer_id=recorded_by),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }


# ============================================================================
# API FUNCTIONS
# ============================================================================

def create(peer_id: str, t_ms: int, db: Any) -> str:
    """Create local-only bootstrap_complete event."""
    from events.identity import peer

    event_data = {
        'type': 'bootstrap_complete',
        'peer_id': peer_id,
        'created_at': t_ms
    }

    private_key = peer.get_private_key(peer_id, peer_id, db)
    if not private_key:
        raise ValueError(f"No private key for peer {peer_id}")

    signed_event = crypto.sign_event(event_data, private_key)
    canonical = crypto.canonicalize_json(signed_event)
    bootstrap_complete_id = store.event(canonical, peer_id, t_ms, db)

    log.info(f"bootstrap_complete.create() created event {bootstrap_complete_id[:20]}...")
    return bootstrap_complete_id


# project_event() handled by generic dispatch (SPEC.generic_dispatch = True)
