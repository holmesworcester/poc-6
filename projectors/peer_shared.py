"""Peer shared projector.

SPEC - declares plaintext, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Peer shared has two verification modes:
- Invite-signed (Phase 3): signed_by == invite_id, links to user_id
- Legacy self-signed: verify with public_key from event (bootstrap only)
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class PeerSharedEventData(TypedDict):
    type: str
    public_key: str  # Base64 encoded
    peer_id: str  # Link to local peer
    created_at: int
    invite_id: NotRequired[str]  # For invite-signed mode
    signed_by: NotRequired[str]  # invite_id for invite-signed, omitted for self-signed


class PeerSharedInput(TypedDict):
    event_id: str
    event_data: PeerSharedEventData
    recorded_by: str
    recorded_at: int
    signature_valid: bool  # From resolver - verified against invite_pubkey or self public_key
    dependencies: dict  # invite: {event_id, event_data} with user_id


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plaintext, signed
    "signer_type": "peer_shared_polymorphic",  # Custom: invite or self
    "dependencies": ["invite:invite?"],  # Optional - only for invite-signed mode
    "tables": ["peers_shared", "peer_self", "valid_events"],
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: PeerSharedInput) -> ProjectorResult:
    """Pure projection: dict -> result. No database access."""
    event_id = input_dict["event_id"]
    event_data: PeerSharedEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    signature_valid = input_dict.get("signature_valid", True)
    deps = input_dict.get("dependencies", {})

    # Validate event type
    if event_data.get("type") != "peer_shared":
        return ProjectorResult(valid=False, reason="Invalid event type")

    # Check required fields
    public_key = event_data.get("public_key")
    if not public_key:
        return ProjectorResult(valid=False, reason="Missing public_key")

    peer_id = event_data.get("peer_id")
    if not peer_id:
        return ProjectorResult(valid=False, reason="Missing peer_id")

    # Determine verification mode
    invite_id = event_data.get("invite_id")
    signed_by = event_data.get("signed_by")
    is_invite_signed = invite_id and signed_by == invite_id

    user_id = None  # Will be set if invite-based

    if is_invite_signed:
        # Invite-signed mode: need invite dependency for user_id
        invite_dep = deps.get("invite")
        if not invite_dep:
            return ProjectorResult(
                blocked=True,
                missing_deps=["invite"],
                reason=f"Waiting for invite {invite_id}"
            )

        # Get user_id from invite
        invite_data = invite_dep.get("event_data", {})
        user_id = invite_data.get("user_id")

        # Signature must be valid (verified by resolver against invite_pubkey)
        if not signature_valid:
            return ProjectorResult(valid=False, reason="Invalid signature (invite key)")
    else:
        # Legacy self-signed mode
        if not signature_valid:
            return ProjectorResult(valid=False, reason="Invalid signature (self-signed)")

    # Build output rows
    tables = {}

    # peers_shared row
    peer_shared_row = {
        "peer_shared_id": event_id,
        "peer_id": peer_id,
        "public_key": public_key,
        "user_id": user_id,  # NULL if self-signed, set if invite-signed
        "created_at": event_data["created_at"],
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }
    tables["peers_shared"] = [peer_shared_row]

    # peer_self row - only for own peer AND invite-signed (has user_id)
    # Self-signed bootstrap should NOT update peer_self
    if peer_id == recorded_by and user_id:
        peer_self_row = {
            "peer_id": peer_id,
            "peer_shared_id": event_id,
            "user_id": user_id,
            "recorded_by": recorded_by,
            "recorded_at": recorded_at,
        }
        tables["peer_self"] = [peer_self_row]

    # valid_events row
    valid_event_row = {
        "event_id": event_id,
        "recorded_by": recorded_by,
    }
    tables["valid_events"] = [valid_event_row]

    return ProjectorResult(valid=True, tables=tables)


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    public_key: str = "pubkey_abc123",
    peer_id: str = "peer_123",
    created_at: int = 1000000,
    invite_id: str = "",
    signed_by: str = "",
) -> dict:
    """Build event_data for testing."""
    result = {
        "type": "peer_shared",
        "public_key": public_key,
        "peer_id": peer_id,
        "created_at": created_at,
    }
    if invite_id:
        result["invite_id"] = invite_id
        result["signed_by"] = signed_by or invite_id  # Default: signed_by = invite_id
    return result


def make_invite_dep(
    event_id: str = "inv_123",
    user_id: str = "user_456",
    mode: str = "peer",
) -> dict:
    """Build invite dependency for testing."""
    return {
        "event_id": event_id,
        "event_data": {
            "type": "invite",
            "mode": mode,
            "user_id": user_id,
            "invite_pubkey": "invite_pubkey_123",
            "created_at": 999000,
        }
    }


def make_input(
    event_id: str = "ps_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    signature_valid: bool = True,
    invite: dict | None = None,
) -> dict:
    """Build complete input dict for testing."""
    # Default: invite-signed mode
    if event_data is None:
        event_data = make_event_data(invite_id="inv_123", peer_id=recorded_by)

    deps = {}
    if invite is not None:
        deps["invite"] = invite
    elif event_data.get("invite_id"):
        # Auto-create invite dep for invite-signed mode
        deps["invite"] = make_invite_dep(event_id=event_data["invite_id"])

    return {
        "event_id": event_id,
        "event_data": event_data,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "signature_valid": signature_valid,
        "dependencies": deps,
    }


def make_self_signed_input(
    event_id: str = "ps_bootstrap",
    peer_id: str = "peer_bootstrap",
    recorded_by: str = "peer_bootstrap",
) -> dict:
    """Build input for legacy self-signed peer_shared (bootstrap)."""
    return make_input(
        event_id=event_id,
        event_data=make_event_data(peer_id=peer_id),  # No invite_id = self-signed
        recorded_by=recorded_by,
        invite=None,
    )
