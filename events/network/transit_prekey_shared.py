"""Transit prekey shared event type (shareable public transit prekey for sync routing)."""
from typing import Any
import logging
import crypto
import store
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,
    "signer_type": "peer_shared",
    "dependencies": [],
    "tables": ["transit_prekeys_shared"],
    "generic_dispatch": True,
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result."""
    from projection import ProjectorResult

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


# ============================================================================
# API FUNCTIONS
# ============================================================================

def create(prekey_id: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any) -> str:
    """Create a shareable transit_prekey_shared event from a local transit prekey.

    Args:
        prekey_id: Local transit_prekey event ID (to get public key from)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        transit_prekey_shared_id: The stored transit_prekey_shared event ID
    """
    log.info(f"transit_prekey_shared.create() creating for prekey_id={prekey_id}, t_ms={t_ms}")

    # Get public key from local transit_prekey event
    prekey_blob = store.get(prekey_id, db)
    if not prekey_blob:
        raise ValueError(f"transit_prekey not found: {prekey_id}")

    prekey_data = crypto.parse_json(prekey_blob)
    prekey_public_b64 = prekey_data['public_key']

    # Create shareable event (signed plaintext)
    # Include transit_prekey_id for linking back during projection
    event_data = {
        'type': 'transit_prekey_shared',
        'transit_prekey_id': prekey_id,
        'peer_id': peer_shared_id,  # Public identity (peer_shared_id)
        'public_key': prekey_public_b64,
        'signed_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext (no encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    transit_prekey_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"transit_prekey_shared.create() created transit_prekey_shared_id={transit_prekey_shared_id}")
    return transit_prekey_shared_id


# project_event() handled by generic dispatch (SPEC.generic_dispatch = True)
