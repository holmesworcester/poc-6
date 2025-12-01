"""Invite accepted projector.

SPEC - declares plaintext, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Invite accepted is a local event capturing invite acceptance.
It stores the invite proof keypair for GKS decryption.
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult, CreateResult, BlobSpec, compute_event_id
import logging
import json
import crypto

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class InviteAcceptedEventData(TypedDict):
    type: str
    invite_id: str  # The invite being accepted
    invite_prekey_id: str  # For GKS decryption
    invite_private_key: str  # Base64 encoded private key
    signed_by: str  # peer_id
    created_at: int


class InviteEventData(TypedDict):
    type: str
    invite_pubkey: str  # Public key to pair with private_key
    inviter_peer_shared_id: NotRequired[str]
    inviter_transit_prekey_id: NotRequired[str]
    inviter_transit_prekey_public_key: NotRequired[str]
    address: NotRequired[str]
    port: NotRequired[int]


class InviteAcceptedInput(TypedDict):
    event_id: str
    event_data: InviteAcceptedEventData
    recorded_by: str
    recorded_at: int
    dependencies: dict  # invite: {event_id, event_data}


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plaintext, local
    "signer_type": "none",  # Local event, no signature verification
    "dependencies": ["invite:invite"],  # Need invite for public key
    "tables": ["group_prekeys", "invite_accepteds", "valid_events"],
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    # All data passed from invite link (out-of-band)
}


# ============================================================================
# CREATE - pure function: deps -> CreateResult
# ============================================================================

class InviteAcceptedCreateDeps(TypedDict):
    """Dependencies for invite_accepted creation (all from invite link)."""
    invite_id: str
    invite_prekey_id: str  # Deterministic prekey ID
    invite_private_key: bytes  # Private key for GKS decryption
    peer_id: str  # Local peer accepting


def create_pure(
    deps: InviteAcceptedCreateDeps,
    t_ms: int,
) -> CreateResult:
    """Pure function to create an invite_accepted event.

    Local-only event capturing invite acceptance with all out-of-band
    data from the invite link for event-sourcing (reprojection).

    Args:
        deps: All invite link data
        t_ms: Timestamp

    Returns:
        CreateResult with invite_accepted blob
    """
    event_data = {
        'type': 'invite_accepted',
        'invite_id': deps['invite_id'],
        'invite_prekey_id': deps['invite_prekey_id'],
        'invite_private_key': crypto.b64encode(deps['invite_private_key']),
        'signed_by': deps['peer_id'],
        'created_at': t_ms,
    }

    # Local-only: plain JSON, no signing
    blob = json.dumps(event_data).encode()
    invite_accepted_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=invite_accepted_id, event_type='invite_accepted')],
        primary_id=invite_accepted_id,
    )


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: InviteAcceptedInput) -> ProjectorResult:
    """Pure projection: dict -> result. No database access."""
    event_id = input_dict["event_id"]
    event_data: InviteAcceptedEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict.get("dependencies", {})

    # Validate event type
    if event_data.get("type") != "invite_accepted":
        return ProjectorResult(valid=False, reason="Invalid event type")

    # Check required fields
    invite_id = event_data.get("invite_id")
    if not invite_id:
        return ProjectorResult(valid=False, reason="Missing invite_id")

    invite_prekey_id = event_data.get("invite_prekey_id")
    if not invite_prekey_id:
        return ProjectorResult(valid=False, reason="Missing invite_prekey_id")

    invite_private_key = event_data.get("invite_private_key")
    if not invite_private_key:
        return ProjectorResult(valid=False, reason="Missing invite_private_key")

    # Get invite dependency for public key
    invite_dep = deps.get("invite")
    if not invite_dep:
        return ProjectorResult(
            blocked=True,
            missing_deps=["invite"],
            reason=f"Waiting for invite {invite_id}"
        )

    invite_event = invite_dep.get("event_data", {})
    invite_pubkey = invite_event.get("invite_pubkey")
    if not invite_pubkey:
        return ProjectorResult(valid=False, reason="Invite missing invite_pubkey")

    # Extract inviter metadata from invite
    inviter_peer_shared_id = (
        invite_event.get("inviter_peer_shared_id") or
        invite_event.get("signed_by") or
        invite_event.get("created_by", "")
    )
    inviter_transit_prekey_id = invite_event.get("inviter_transit_prekey_id")
    inviter_transit_prekey_public_key = invite_event.get("inviter_transit_prekey_public_key")
    address = invite_event.get("address")
    port = invite_event.get("port")

    # Build output rows
    tables = {}

    # group_prekeys row - store invite proof keypair for GKS decryption
    prekey_row = {
        "prekey_id": invite_prekey_id,
        "owner_peer_id": recorded_by,
        "public_key": invite_pubkey,  # Will be decoded in wrapper
        "private_key": invite_private_key,  # Will be decoded in wrapper
        "created_at": event_data["created_at"],
        "recorded_by": recorded_by,
    }
    tables["group_prekeys"] = [prekey_row]

    # invite_accepteds row - store inviter metadata for bootstrap connections
    invite_accepted_row = {
        "invite_id": invite_id,
        "inviter_peer_shared_id": inviter_peer_shared_id,
        "address": address,
        "port": port,
        "inviter_transit_prekey_id": inviter_transit_prekey_id,
        "inviter_transit_prekey_public_key": inviter_transit_prekey_public_key,  # Will be decoded in wrapper
        "created_at": event_data["created_at"],
        "recorded_by": recorded_by,
    }
    tables["invite_accepteds"] = [invite_accepted_row]

    # valid_events rows - both invite_accepted and invite
    tables["valid_events"] = [
        {"event_id": event_id, "recorded_by": recorded_by},
        {"event_id": invite_id, "recorded_by": recorded_by},
    ]

    return ProjectorResult(valid=True, tables=tables)


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    invite_id: str = "inv_123",
    invite_prekey_id: str = "prekey_123",
    invite_private_key: str = "private_key_b64",
    signed_by: str = "peer_456",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "invite_accepted",
        "invite_id": invite_id,
        "invite_prekey_id": invite_prekey_id,
        "invite_private_key": invite_private_key,
        "signed_by": signed_by,
        "created_at": created_at,
    }


def make_invite_dep(
    event_id: str = "inv_123",
    invite_pubkey: str = "invite_pubkey_123",
    inviter_peer_shared_id: str = "ps_inviter",
    inviter_transit_prekey_id: str = "transit_prekey_123",
    address: str = "127.0.0.1",
    port: int = 6100,
) -> dict:
    """Build invite dependency for testing."""
    return {
        "event_id": event_id,
        "event_data": {
            "type": "invite",
            "invite_pubkey": invite_pubkey,
            "inviter_peer_shared_id": inviter_peer_shared_id,
            "inviter_transit_prekey_id": inviter_transit_prekey_id,
            "inviter_transit_prekey_public_key": "transit_pubkey_b64",
            "address": address,
            "port": port,
            "created_at": 999000,
        }
    }


def make_input(
    event_id: str = "ia_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    invite: dict | None = None,
) -> dict:
    """Build complete input dict for testing."""
    if event_data is None:
        event_data = make_event_data()

    deps = {}
    if invite is not None:
        deps["invite"] = invite
    else:
        deps["invite"] = make_invite_dep(event_id=event_data.get("invite_id", "inv_123"))

    return {
        "event_id": event_id,
        "event_data": event_data,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": deps,
    }
