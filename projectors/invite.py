"""Invite projector.

SPEC - declares plaintext, signer_type, dependencies, tables
project() - pure function: dict -> ProjectorResult
make_input(), make_event_data() - composable test builders

Invite has polymorphic signing:
- Bootstrap: signed_by == network_id (verified with network pubkey)
- Ongoing: signed_by == peer_shared_id (verified with peer_shared pubkey)
- Legacy: no signed_by, uses created_by (verified with peer_shared pubkey)
"""

from typing import TypedDict, NotRequired
from projectors import ProjectorResult
import logging

log = logging.getLogger(__name__)


# ============================================================================
# TYPES - for autocomplete inside event dicts
# ============================================================================

class InviteEventData(TypedDict):
    type: str
    mode: NotRequired[str]  # 'user' or 'peer', defaults to 'user'
    invite_pubkey: str  # For user proof signature
    network_id: NotRequired[str]  # Network this invite is for
    group_id: NotRequired[str]  # Target group (None for mode='peer')
    channel_id: NotRequired[str]  # Target channel
    key_id: NotRequired[str]  # Group key
    user_id: NotRequired[str]  # For mode='peer': target user to link to
    signed_by: NotRequired[str]  # network_id (bootstrap) or peer_shared_id (ongoing)
    created_by: NotRequired[str]  # Legacy: inviter's peer_shared_id
    inviter_peer_shared_id: NotRequired[str]  # Inviter's peer_shared_id
    inviter_user_id: NotRequired[str]  # Inviter's user_id
    admin_grant: NotRequired[str]  # For ongoing invites: admin_id authorizing signer
    created_at: int


class InviteInput(TypedDict):
    event_id: str
    event_data: InviteEventData
    recorded_by: str
    recorded_at: int
    signature_valid: bool  # From resolver
    dependencies: dict  # signer_user, admin_grant


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plaintext, signed
    "signer_type": "invite_polymorphic",  # Custom: network or peer_shared
    "dependencies": [
        "signer_user:linked_peer?",  # For ongoing invites
        "admin_grant:admin_grant?",  # For ongoing invites with admin_grant
    ],
    "tables": ["invites", "valid_events"],
}


# ============================================================================
# PROJECTOR - pure function: dict -> ProjectorResult
# ============================================================================

def project(input_dict: InviteInput) -> ProjectorResult:
    """Pure projection: dict -> result. No database access."""
    event_id = input_dict["event_id"]
    event_data: InviteEventData = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]
    signature_valid = input_dict.get("signature_valid", True)
    deps = input_dict.get("dependencies", {})

    # Validate event type
    if event_data.get("type") != "invite":
        return ProjectorResult(valid=False, reason="Invalid event type")

    # Check required fields
    invite_pubkey = event_data.get("invite_pubkey")
    if not invite_pubkey:
        return ProjectorResult(valid=False, reason="Missing invite_pubkey")

    # Signature must be valid (verified by resolver)
    if not signature_valid:
        return ProjectorResult(valid=False, reason="Invalid signature")

    # Determine mode
    mode = event_data.get("mode", "user")
    signed_by = event_data.get("signed_by")
    network_id = event_data.get("network_id")

    # For ongoing invites (not bootstrap), validate admin_grant chain
    # Note: check_deps() in recorded.py already blocks until admin_grant is valid
    if signed_by and signed_by != network_id:
        # Ongoing invite: validate admin_grant if present
        admin_grant_id = event_data.get("admin_grant")

        if admin_grant_id:
            # admin_grant specified - verify it authorizes the signer
            admin_grant = deps.get("admin_grant")
            signer_user = deps.get("signer_user")

            # admin_grant should be available (check_deps blocked until valid)
            # signer_user may not be available if peer_shared doesn't have user_id yet
            if admin_grant and signer_user:
                signer_user_id = signer_user.get("user_id")
                grant_user_id = admin_grant.get("user_id")

                if signer_user_id != grant_user_id:
                    return ProjectorResult(
                        valid=False,
                        reason=f"admin_grant does not authorize signer (grant grants {grant_user_id}, signer is {signer_user_id})"
                    )
                log.debug(f"invite.project() admin_grant chain verified")
            elif admin_grant and not signer_user:
                # signer_user not available - skip validation for backward compatibility
                # TODO: Consider making this stricter once all peer_shared have user_id
                log.debug(f"invite.project() skipping admin_grant validation: signer_user not available")

    # Determine inviter_id
    inviter_id = (
        event_data.get("inviter_peer_shared_id") or
        event_data.get("signed_by") or
        event_data.get("created_by") or
        event_data.get("first_peer", "")
    )

    # Build output rows
    tables = {}

    # invites row
    invite_row = {
        "invite_id": event_id,
        "invite_pubkey": invite_pubkey,
        "group_id": event_data.get("group_id"),  # May be None for mode='peer'
        "inviter_id": inviter_id,
        "mode": mode,
        "user_id": event_data.get("user_id"),  # For mode='peer'
        "created_at": event_data["created_at"],
        "recorded_by": recorded_by,
    }
    tables["invites"] = [invite_row]

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
    mode: str = "user",
    invite_pubkey: str = "invite_pubkey_123",
    network_id: str = "net_123",
    group_id: str = "grp_all_users",
    channel_id: str = "ch_123",
    key_id: str = "key_123",
    signed_by: str = "net_123",  # Default: bootstrap (network-signed)
    inviter_peer_shared_id: str = "ps_inviter",
    admin_grant: str = "",
    user_id: str = "",  # For mode='peer'
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    result = {
        "type": "invite",
        "mode": mode,
        "invite_pubkey": invite_pubkey,
        "network_id": network_id,
        "signed_by": signed_by,
        "inviter_peer_shared_id": inviter_peer_shared_id,
        "created_at": created_at,
    }
    if group_id:
        result["group_id"] = group_id
    if channel_id:
        result["channel_id"] = channel_id
    if key_id:
        result["key_id"] = key_id
    if admin_grant:
        result["admin_grant"] = admin_grant
    if user_id:
        result["user_id"] = user_id
    return result


def make_input(
    event_id: str = "inv_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    signature_valid: bool = True,
    signer_user: dict | None = None,
    admin_grant: dict | None = None,
) -> dict:
    """Build complete input dict for testing."""
    if event_data is None:
        event_data = make_event_data()

    deps = {}
    if signer_user is not None:
        deps["signer_user"] = signer_user
    if admin_grant is not None:
        deps["admin_grant"] = admin_grant

    return {
        "event_id": event_id,
        "event_data": event_data,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "signature_valid": signature_valid,
        "dependencies": deps,
    }


def make_bootstrap_input(
    event_id: str = "inv_bootstrap",
    network_id: str = "net_123",
) -> dict:
    """Build input for bootstrap invite (network-signed)."""
    return make_input(
        event_id=event_id,
        event_data=make_event_data(
            signed_by=network_id,  # Bootstrap: signed by network
            network_id=network_id,
        ),
    )


def make_ongoing_input(
    event_id: str = "inv_ongoing",
    signer_peer_shared_id: str = "ps_inviter",
    signer_user_id: str = "user_inviter",
    admin_grant_id: str = "admin_123",
) -> dict:
    """Build input for ongoing invite (peer_shared-signed with admin_grant)."""
    return make_input(
        event_id=event_id,
        event_data=make_event_data(
            signed_by=signer_peer_shared_id,  # Ongoing: signed by peer_shared
            admin_grant=admin_grant_id,
        ),
        signer_user={"user_id": signer_user_id, "peer_id": signer_peer_shared_id},
        admin_grant={"event_id": admin_grant_id, "user_id": signer_user_id},
    )


def make_peer_invite_input(
    event_id: str = "inv_peer",
    user_id: str = "user_target",
    signer_user_id: str = "user_target",  # First peer: signed by user
) -> dict:
    """Build input for mode='peer' invite (device linking)."""
    return make_input(
        event_id=event_id,
        event_data=make_event_data(
            mode="peer",
            signed_by=user_id,  # First peer: signed by user_id
            user_id=user_id,  # Target user
            group_id="",  # No group for peer invites
        ),
    )
