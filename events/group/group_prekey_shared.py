"""Prekey shared event type (shareable public prekey for group key wrapping).

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    create(prekey_id, peer_id, peer_shared_id, t_ms, db, ...) -> str
"""
from typing import Any, TypedDict, NotRequired
import logging
import crypto
import store
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class GroupPrekeySharedEventData(TypedDict):
    type: str
    group_prekey_id: str
    peer_id: str  # peer_shared_id
    public_key: str  # base64 encoded
    signed_by: str
    created_at: int
    group_id: NotRequired[str]  # For mode=user invites
    key_id: NotRequired[str]    # For mode=user invites
    user_id: NotRequired[str]   # For mode=link invites


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,
    "signer_type": "peer_shared",
    "dependencies": [],
    "tables": ["group_prekeys_shared"],
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

    peer_id = event_data.get("peer_id")
    public_key_b64 = event_data.get("public_key")
    created_at = event_data.get("created_at")

    if not all([peer_id, public_key_b64, created_at]):
        return ProjectorResult(valid=False, reason="Missing required fields")

    public_key = crypto.b64decode(public_key_b64)

    row = {
        "group_prekey_shared_id": event_id,
        "peer_id": peer_id,
        "public_key": public_key,
        "created_at": created_at,
        "recorded_by": recorded_by,
    }

    return ProjectorResult(valid=True, tables={"group_prekeys_shared": [row]})


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    group_prekey_id: str = "gpk_123",
    peer_id: str = "ps_123",
    public_key: str = "cHVibGljX2tleV9ieXRlcw==",
    signed_by: str = "ps_123",
    created_at: int = 1000000,
    group_id: str | None = "grp_123",
    key_id: str | None = "key_123",
    user_id: str | None = None,
) -> dict:
    data = {
        "type": "group_prekey_shared",
        "group_prekey_id": group_prekey_id,
        "peer_id": peer_id,
        "public_key": public_key,
        "signed_by": signed_by,
        "created_at": created_at,
    }
    if group_id:
        data["group_id"] = group_id
    if key_id:
        data["key_id"] = key_id
    if user_id:
        data["user_id"] = user_id
    return data


def make_input(
    event_id: str = "gpks_123",
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


# ============================================================================
# API FUNCTIONS
# ============================================================================

def create(prekey_id: str, peer_id: str, peer_shared_id: str,
           t_ms: int, db: Any,
           group_id: str | None = None, key_id: str | None = None,
           user_id: str | None = None,
           wrap_key_data: dict | None = None) -> str:
    """Create a shareable group_prekey_shared event from a local group prekey.

    Context must be either group-based (for user invites) or user-based (for link invites).
    Exactly one context type must be provided.

    Args:
        prekey_id: Local group_prekey event ID (to get public key from)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection
        group_id: Group context (for mode=user invites). Requires key_id.
        key_id: Key reference (for mode=user invites). Requires group_id.
        user_id: User context (for mode=link invites - device linking)
        wrap_key_data: Optional key dict for wrapping (used when network key not available yet)

    Returns:
        group_prekey_shared_id: The stored group_prekey_shared event ID

    Raises:
        ValueError: If neither group context nor user context is provided,
                    or if both are provided
    """
    # Validate context - must have exactly one type
    has_group_context = group_id is not None
    has_user_context = user_id is not None

    if not has_group_context and not has_user_context:
        raise ValueError("group_prekey_shared requires either group context (group_id, key_id) or user context (user_id)")
    if has_group_context and has_user_context:
        raise ValueError("group_prekey_shared cannot have both group context and user context")
    if has_group_context and key_id is None:
        raise ValueError("group context requires both group_id and key_id")

    log.info(f"group_prekey_shared.create() creating group_prekey_shared for prekey_id={prekey_id}, t_ms={t_ms}")

    # Get public key from local prekey event
    prekey_blob = store.get(prekey_id, db)
    if not prekey_blob:
        raise ValueError(f"prekey not found: {prekey_id}")

    prekey_data = crypto.parse_json(prekey_blob)
    prekey_public_b64 = prekey_data['public_key']

    # Create shareable event (encrypted + signed)
    # Include group_prekey_id for linking back during projection
    event_data = {
        'type': 'group_prekey_shared',
        'group_prekey_id': prekey_id,
        'peer_id': peer_shared_id,
        'public_key': prekey_public_b64,
        'signed_by': peer_shared_id,
        'created_at': t_ms,
    }

    # Add context fields based on invite type:
    # - mode=user: group context (group_id, key_id)
    # - mode=link: user context (user_id)
    if has_group_context:
        event_data['group_id'] = group_id
        event_data['key_id'] = key_id
    else:
        event_data['user_id'] = user_id

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext (no inner encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    group_prekey_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_prekey_shared.create() created group_prekey_shared_id={group_prekey_shared_id}")
    return group_prekey_shared_id


# project_event() handled by generic dispatch (SPEC.generic_dispatch = True)
