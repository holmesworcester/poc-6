"""Admin projector.

SPEC - declares encrypted, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Admin events have polymorphic signing:
- Bootstrap: signed_by == network_id (verify with network_pubkey)
- Ongoing: signed_by == peer_shared_id (verify with peer pubkey + admin_grant chain)
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult, CreateResult, BlobSpec, compute_event_id
import logging
import crypto

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class AdminEventData(TypedDict):
    type: str
    user_id: str
    network_id: str
    signed_by: str
    created_at: int
    admin_grant: NotRequired[str]


class NetworkDep(TypedDict):
    network_id: str
    network_pubkey: bytes


class SignerUserDep(TypedDict):
    user_id: str
    peer_id: str


class AdminGrantDep(TypedDict):
    event_id: str
    user_id: str


class AdminInput(TypedDict):
    event_id: str
    event_data: AdminEventData
    recorded_by: str
    recorded_at: int
    signature_valid: bool  # Pre-computed by resolver
    dependencies: dict  # {"network": NetworkDep | None, "signer_user": SignerUserDep | None, "admin_grant": AdminGrantDep | None}


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Admin events are plaintext
    "signer_type": "admin",  # Special: polymorphic (network or peer_shared)
    "dependencies": ["signer_user:linked_peer?", "admin_grant:admin_grant?"],
    "tables": ["admins"],
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    # All deps are passed as args (user_id, network_id, signer info)
    # No table lookups needed
}


# ============================================================================
# CREATE - pure function: deps -> CreateResult
# ============================================================================

class AdminCreateDeps(TypedDict):
    """Dependencies for admin creation (all passed as args)."""
    user_id: str
    network_id: str
    signed_by: str  # network_id for bootstrap, peer_shared_id for ongoing
    signer_private_key: bytes
    admin_grant: NotRequired[str]  # Prior admin_id for chain (None for bootstrap)


def create_pure(
    deps: AdminCreateDeps,
    t_ms: int,
) -> CreateResult:
    """Pure function to create an admin event.

    Admin events are plaintext (not encrypted) and signed by either:
    - network_id (bootstrap): First admin grant
    - peer_shared_id (ongoing): Subsequent admin grants with admin_grant chain

    Args:
        deps: Resolved dependencies (all passed as args)
        t_ms: Timestamp

    Returns:
        CreateResult with admin blob
    """
    event_data = {
        'type': 'admin',
        'user_id': deps['user_id'],
        'network_id': deps['network_id'],
        'signed_by': deps['signed_by'],
        'created_at': t_ms,
    }

    if deps.get('admin_grant'):
        event_data['admin_grant'] = deps['admin_grant']

    # Sign the event
    signed_event = crypto.sign_event(event_data, deps['signer_private_key'])

    # Canonicalize (no encryption - admin events are plaintext)
    blob = crypto.canonicalize_json(signed_event)
    admin_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=admin_id, event_type='admin')],
        primary_id=admin_id,
    )


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: AdminInput) -> ProjectorResult:
    """Pure projection: dict -> result. No database access."""
    event_id = input_dict["event_id"]
    event_data: AdminEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict["dependencies"]

    # Validate event type
    if event_data.get("type") != "admin":
        return ProjectorResult(valid=False, reason="Invalid event type")

    signed_by = event_data["signed_by"]
    network_id = event_data["network_id"]
    user_id = event_data["user_id"]
    admin_grant_id = event_data.get("admin_grant")

    # Check signature was valid (pre-computed by resolver)
    if not input_dict.get("signature_valid"):
        return ProjectorResult(valid=False, reason="Signature verification failed")

    # Determine if bootstrap or ongoing
    is_bootstrap = (signed_by == network_id)

    if not is_bootstrap:
        # Ongoing: need admin_grant field and signer_user dependency
        if not admin_grant_id:
            return ProjectorResult(valid=False, reason="Ongoing admin grant requires admin_grant reference")

        signer_user = deps.get("signer_user")
        if not signer_user:
            return ProjectorResult(blocked=True, missing_deps=["signer_user"])

        admin_grant = deps.get("admin_grant")
        if not admin_grant:
            return ProjectorResult(blocked=True, missing_deps=["admin_grant"])

        # Verify admin_grant authorizes the signer's user
        if admin_grant["user_id"] != signer_user["user_id"]:
            return ProjectorResult(
                valid=False,
                reason=f"admin_grant does not authorize signer {signer_user['user_id']}"
            )

    # Build output row
    admin_row = {
        "admin_id": event_id,
        "user_id": user_id,
        "network_id": network_id,
        "signed_by": signed_by,
        "admin_grant": admin_grant_id,
        "created_at": event_data["created_at"],
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(valid=True, tables={"admins": [admin_row]})


# ============================================================================
# TEST BUILDERS - compose these to create test inputs (no DB required)
# ============================================================================

def make_event_data(
    user_id: str = "user_123",
    network_id: str = "net_123",
    signed_by: str | None = None,  # Defaults to network_id (bootstrap)
    created_at: int = 1000000,
    admin_grant: str | None = None,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "admin",
        "user_id": user_id,
        "network_id": network_id,
        "signed_by": signed_by or network_id,
        "created_at": created_at,
        "admin_grant": admin_grant,
    }


def make_input(
    event_id: str = "admin_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    signature_valid: bool = True,
    signer_user: dict | None = None,
    admin_grant: dict | None = None,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "signature_valid": signature_valid,
        "dependencies": {
            "signer_user": signer_user,
            "admin_grant": admin_grant,
        },
    }
