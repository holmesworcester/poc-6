"""Admin event type (shareable, plaintext) - grants admin status to a user.

Admin status is granted by:
- Bootstrap: signed_by=network_id (verified with network_pubkey)
- Ongoing: signed_by=peer_shared_id (verified with peer.pubkey + admin_grant chain)

SPEC/DEPS - declarative metadata for generic resolver
project() - pure function: input_dict -> ProjectorResult
create_pure() - pure function: deps -> CreateResult

API functions:
    create(...) -> str
    project_event(admin_id, recorded_by, recorded_at, db) -> str | None
"""
from typing import Any, TypedDict, NotRequired
import logging
import crypto
import store
from db import create_safe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class AdminEventData(TypedDict):
    type: str
    user_id: str
    network_id: str
    signed_by: str
    created_at: int
    admin_grant: NotRequired[str]


class SignerUserDep(TypedDict):
    user_id: str
    peer_id: str


class AdminGrantDep(TypedDict):
    event_id: str
    user_id: str


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Admin events are plaintext
    "signer_type": "admin",  # Polymorphic: network_id OR peer_shared_id
    "dependencies": ["signer_user:linked_peer?", "admin_grant:admin_grant?"],
    "tables": ["admins"],
    "generic_dispatch": True,
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    # All deps passed as args (user_id, network_id, signer info)
    # No table lookups needed
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> ProjectorResult. No database access."""
    from projection import ProjectorResult

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    deps = input_dict.get("dependencies", {})

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

    # For bootstrap admin, include command to set creator_user_id
    commands = []
    if is_bootstrap:
        commands.append({
            "type": "set_network_creator",
            "network_id": network_id,
            "user_id": user_id,
        })

    return ProjectorResult(valid=True, tables={"admins": [admin_row]}, commands=commands)


def create_pure(
    user_id: str,
    network_id: str,
    signed_by: str,
    signer_private_key: bytes,
    t_ms: int,
    admin_grant: str | None = None,
):
    """Pure function to create an admin event.

    Args:
        user_id: The user being granted admin
        network_id: The network this admin grant is for
        signed_by: Either network_id (bootstrap) or peer_shared_id (ongoing)
        signer_private_key: Private key corresponding to signed_by
        t_ms: Timestamp
        admin_grant: Prior admin_id for authorization chain (None for bootstrap)

    Returns:
        CreateResult with admin blob
    """
    from projection import CreateResult, BlobSpec, compute_event_id

    event_data = {
        'type': 'admin',
        'user_id': user_id,
        'network_id': network_id,
        'signed_by': signed_by,
        'created_at': t_ms,
    }

    if admin_grant:
        event_data['admin_grant'] = admin_grant

    # Sign the event
    signed_event = crypto.sign_event(event_data, signer_private_key)

    # Canonicalize (no encryption - admin events are plaintext)
    blob = crypto.canonicalize_json(signed_event)
    admin_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=admin_id, event_type='admin')],
        primary_id=admin_id,
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    user_id: str = "user_123",
    network_id: str = "net_123",
    signed_by: str | None = None,
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


# ============================================================================
# API FUNCTIONS
# ============================================================================

def create(
    user_id: str,
    network_id: str,
    signed_by: str,
    signer_private_key: bytes,
    t_ms: int,
    peer_id: str,
    db: Any,
    admin_grant: str | None = None
) -> str:
    """Create an admin event granting admin status to a user.

    Args:
        user_id: The user being granted admin
        network_id: The network this admin grant is for
        signed_by: Either network_id (bootstrap) or peer_shared_id (ongoing)
        signer_private_key: Private key corresponding to signed_by
        t_ms: Timestamp
        peer_id: Local peer ID (for recording)
        db: Database connection
        admin_grant: Prior admin_id for authorization chain (None for bootstrap)

    Returns:
        admin_id: The ID of the created admin event
    """
    from projection import store_create_result

    result = create_pure(user_id, network_id, signed_by, signer_private_key, t_ms, admin_grant)
    admin_id = store_create_result(result, peer_id, t_ms, db)

    log.info(f"admin.create() created admin grant: admin_id={admin_id[:20]}..., "
             f"user_id={user_id[:20]}..., network_id={network_id[:20]}...")

    return admin_id


def project_event(admin_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project admin event into admins table.

    Uses generic resolver + pure projector + apply_result.
    """
    from projection import resolve, apply_result

    input_dict = resolve("admin", admin_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = project(input_dict)

    if result.blocked:
        log.debug(f"admin.project_event() blocked, missing: {result.missing_deps}")
        return None

    if not result.valid:
        log.warning(f"admin.project_event() invalid: {result.reason}")
        return None

    apply_result(result, recorded_by, recorded_at, db)

    # Handle commands (side effects that pure projector can't do)
    for cmd in result.commands:
        if cmd["type"] == "set_network_creator":
            safedb = create_safe_db(db, recorded_by=recorded_by)
            safedb.execute(
                """UPDATE networks SET creator_user_id = ?
                   WHERE network_id = ? AND recorded_by = ?
                   AND (creator_user_id IS NULL OR creator_user_id = '')""",
                (cmd["user_id"], cmd["network_id"], recorded_by)
            )
            log.info(f"admin.project_event() set creator_user_id={cmd['user_id'][:20]}...")

    log.info(f"admin.project_event() projected admin_id={admin_id[:20]}...")
    return admin_id


def is_user_admin(user_id: str, network_id: str, recorded_by: str, db: Any) -> bool:
    """Check if a user has admin status in a network."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    admin_row = safedb.query_one(
        "SELECT 1 FROM admins WHERE user_id = ? AND network_id = ? AND recorded_by = ?",
        (user_id, network_id, recorded_by)
    )
    return admin_row is not None


def my_grant(user_id: str, network_id: str, recorded_by: str, db: Any) -> str | None:
    """Get the admin_id that granted admin to a user.

    Used for creating admin_grant chain when granting admin to others.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    admin_row = safedb.query_one(
        "SELECT admin_id FROM admins WHERE user_id = ? AND network_id = ? AND recorded_by = ?",
        (user_id, network_id, recorded_by)
    )
    return admin_row['admin_id'] if admin_row else None
