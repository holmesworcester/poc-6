"""Invite accepted event type (local-only, captures invite acceptance).

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    create(invite_id, invite_prekey_id, invite_private_key, peer_id, t_ms, db) -> str
    project_event(invite_accepted_id, recorded_by, recorded_at, db) -> str | None
"""
from typing import Any, TypedDict, NotRequired
import json
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class InviteAcceptedEventData(TypedDict):
    type: str
    invite_id: str
    invite_prekey_id: str
    invite_private_key: str  # Base64 encoded
    signed_by: str  # peer_id
    created_at: int


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plaintext, local
    "signer_type": "none",  # Local event, no signature verification
    "dependencies": ["invite:invite"],  # Need invite for public key
    "tables": ["group_prekeys", "invite_accepteds", "valid_events"],
    "generic_dispatch": True,
    "mark_valid": True,
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result. No database access."""
    from projection import ProjectorResult

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
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
        "public_key": invite_pubkey,
        "private_key": invite_private_key,
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
        "inviter_transit_prekey_public_key": inviter_transit_prekey_public_key,
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
# TEST BUILDERS
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


# ============================================================================
# API FUNCTIONS
# ============================================================================


def create(invite_id: str, invite_prekey_id: str, invite_private_key: bytes,
           peer_id: str, t_ms: int, db: Any) -> str:
    """Create local invite_accepted event (not shareable).

    This event captures the invite acceptance action and stores ALL
    out-of-band data from the invite link for event-sourcing (reprojection).

    Args:
        invite_id: The invite event being accepted
        invite_prekey_id: Deterministic prekey ID for storing invite proof keypair
        invite_private_key: Private key for GKS decryption + invite proof signature
        peer_id: Bob's peer_id (local)
        t_ms: Timestamp
        db: Database connection

    Returns:
        invite_accepted_id: Event ID
    """
    log.info(f"invite_accepted.create() for invite={invite_id}, peer={peer_id}")

    event_data = {
        'type': 'invite_accepted',
        'invite_id': invite_id,
        'invite_prekey_id': invite_prekey_id,
        'invite_private_key': crypto.b64encode(invite_private_key),
        'signed_by': peer_id,
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store with recorded wrapper and projection
    invite_accepted_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"invite_accepted.create() created invite_accepted_id={invite_accepted_id}")
    return invite_accepted_id


def project_event(invite_accepted_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project invite_accepted: restore ALL invite link data for event-sourcing.

    This restores the invite_transit_key from the invite link and enables
    full reprojection without the original invite link.
    """
    log.warning(f"[INVITE_ACCEPTED_PROJECT_ENTRY] id={invite_accepted_id[:20]}..., recorded_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(invite_accepted_id, unsafedb)
    if not blob:
        log.warning(f"invite_accepted.project() blob not found")
        return None

    event_data = crypto.parse_json(blob)

    # Extract invite proof keypair data from invite_accepted and invite events
    invite_id = event_data['invite_id']
    invite_prekey_id = event_data['invite_prekey_id']  # From invite link
    invite_private_key = crypto.b64decode(event_data['invite_private_key'])

    # Get invite event to extract public key
    invite_blob = store.get(invite_id, unsafedb)
    if not invite_blob:
        log.warning(f"invite_accepted.project() invite blob not found: {invite_id}")
        return None

    invite_event = crypto.parse_json(invite_blob)
    log.info(f"[INVITE_ACCEPTED_PROJECT] invite event keys={list(invite_event.keys())}")
    invite_public_key = crypto.b64decode(invite_event['invite_pubkey'])

    # Store invite proof keypair in group_prekeys table (for GKS decryption)
    # Use invite_prekey_id as prekey_id (matches hint in GKS blob)
    safedb.execute(
        """INSERT OR IGNORE INTO group_prekeys
           (prekey_id, owner_peer_id, public_key, private_key, created_at, recorded_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            invite_prekey_id,  # Deterministic prekey ID (matches GKS hint)
            recorded_by,
            invite_public_key,
            invite_private_key,
            event_data['created_at'],
            recorded_by
        )
    )

    log.warning(f"[INVITE_ACCEPTED_PROJECT] stored invite_private_key prekey_id={invite_prekey_id[:20]}... for peer {recorded_by[:20]}...")

    # Unblock events that were waiting for this prekey (e.g., group_key_shared events sealed to this invite)
    import queues
    from events.network import recorded as recorded_module
    unblocked_ids = queues.blocked.notify_event_valid(invite_prekey_id, recorded_by, safedb)
    if unblocked_ids:
        log.info(f"invite_accepted.project() unblocked {len(unblocked_ids)} events waiting for invite prekey")
        recorded_module.project_ids(unblocked_ids, db)

    # Mark invite_accepted as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_accepted_id, recorded_by)
    )

    # Mark the invite itself as valid (restores out-of-band trust from invite link)
    # This is necessary for reprojection since the invite link is not available
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_id, recorded_by)
    )

    # Store inviter metadata in invite_accepteds table BEFORE bootstrap check
    # This table entry signals that bootstrap has been initiated
    # Get inviter's peer_shared_id from invite event
    # For new invites: use inviter_peer_shared_id field
    # For legacy: fall back to created_by, then signed_by
    inviter_peer_shared_id = invite_event.get('inviter_peer_shared_id') or invite_event.get('signed_by') or invite_event.get('created_by')
    inviter_transit_prekey_id = invite_event.get('inviter_transit_prekey_id')
    inviter_transit_prekey_public_key = None

    if inviter_transit_prekey_id:
        inviter_transit_prekey_public_key = crypto.b64decode(
            invite_event.get('inviter_transit_prekey_public_key', '')
        )

    # Extract address/port from invite event (for bootstrap connections)
    # These fields allow send_connect_to_all() to connect to inviter before sync completes
    address = invite_event.get('address')
    port = invite_event.get('port')

    safedb.execute("""
        INSERT OR IGNORE INTO invite_accepteds
        (invite_id, inviter_peer_shared_id, address, port,
         inviter_transit_prekey_id, inviter_transit_prekey_public_key,
         created_at, recorded_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        invite_id,
        inviter_peer_shared_id,
        address,  # Now extracted from invite event
        port,     # Now extracted from invite event
        inviter_transit_prekey_id,
        inviter_transit_prekey_public_key,
        event_data['created_at'],
        recorded_by
    ))

    # Bootstrap invites use signed_by=network_id which doesn't need artificial blocking
    # Admin privileges are granted via admin event created in new_network()

    # Unblock events waiting for the invite
    unblocked_by_invite = queues.blocked.notify_event_valid(invite_id, recorded_by, safedb)
    if unblocked_by_invite:
        log.info(f"invite_accepted.project() unblocked {len(unblocked_by_invite)} events waiting for invite")
        recorded_module.project_ids(unblocked_by_invite, db)

    log.info(f"invite_accepted.project() completed for {recorded_by}")
    return invite_accepted_id
