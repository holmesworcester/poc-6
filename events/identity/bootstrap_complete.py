"""Bootstrap complete event type - marks when a joiner receives first sync request from network.

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    create(peer_id, t_ms, db) -> str
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

class BootstrapCompleteEventData(TypedDict):
    type: str
    peer_id: str
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

    if not peer_id:
        return ProjectorResult(valid=False, reason="Missing peer_id")

    # Only project our own bootstrap_complete event
    if recorded_by != peer_id:
        # Foreign event - valid but no tables to write
        return ProjectorResult(valid=True, tables={})

    row = {
        "peer_id": peer_id,
        "recorded_by": recorded_by,
    }

    return ProjectorResult(valid=True, tables={"bootstrap_completers": [row]})


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    peer_id: str = "peer_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "bootstrap_complete",
        "peer_id": peer_id,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "bc_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_123",  # Must match peer_id for projection to occur
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
    """Create local-only bootstrap_complete event.

    This is created when a joiner receives their first sync request from the network,
    indicating they have successfully bootstrapped.

    Args:
        peer_id: The peer who has completed bootstrap
        t_ms: Timestamp
        db: Database connection

    Returns:
        bootstrap_complete event ID
    """
    from events.identity import peer

    # Create event data (local-only, so minimal fields)
    event_data = {
        'type': 'bootstrap_complete',
        'peer_id': peer_id,
        'created_at': t_ms
    }

    # Get peer's private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)
    if not private_key:
        log.error(f"bootstrap_complete.create() no private key for peer {peer_id[:20]}...")
        raise ValueError(f"No private key for peer {peer_id}")

    # Sign the event
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext (local-only)
    canonical = crypto.canonicalize_json(signed_event)
    bootstrap_complete_id = store.event(canonical, peer_id, t_ms, db)

    log.info(f"bootstrap_complete.create() created event {bootstrap_complete_id[:20]}... for peer {peer_id[:20]}...")

    return bootstrap_complete_id


def project_event(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project bootstrap_complete event into bootstrap_completers table."""
    from projectors import resolve, apply_result
    from projectors import bootstrap_complete as bc_projector

    input_dict = resolve("bootstrap_complete", event_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = bc_projector.project(input_dict)

    if not result.valid:
        log.warning(f"bootstrap_complete.project() failed: {result.reason}")
        return None

    apply_result(result, recorded_by, recorded_at, db)
    return event_id
