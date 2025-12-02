"""Invite event type (shareable, encrypted).

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    is_admin(peer_shared_id, recorded_by, db) -> bool
    create(...) -> tuple[str, str, dict]
    project_event(invite_id, recorded_by, recorded_at, db, skip_admin_check) -> str | None
    create_peer_invite(...) -> tuple[str, bytes, bytes]
    accept(invite_link, peer_id, name, t_ms, db) -> dict
"""
from typing import Any, TypedDict, NotRequired
import secrets
import json
import logging
import crypto
import store
from events.network import transit_key
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
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
    if signed_by and signed_by != network_id:
        admin_grant_id = event_data.get("admin_grant")

        if admin_grant_id:
            admin_grant = deps.get("admin_grant")
            signer_user = deps.get("signer_user")

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
        "group_id": event_data.get("group_id"),
        "inviter_id": inviter_id,
        "mode": mode,
        "user_id": event_data.get("user_id"),
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
# TEST BUILDERS
# ============================================================================

def make_event_data(
    mode: str = "user",
    invite_pubkey: str = "invite_pubkey_123",
    network_id: str = "net_123",
    group_id: str = "grp_all_users",
    channel_id: str = "ch_123",
    key_id: str = "key_123",
    signed_by: str = "net_123",
    inviter_peer_shared_id: str = "ps_inviter",
    admin_grant: str = "",
    user_id: str = "",
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


# ============================================================================
# API FUNCTIONS
# ============================================================================


def is_admin(peer_shared_id: str, recorded_by: str, db: Any) -> bool:
    """Check if a peer is an admin (centralized admin validation).

    A peer is an admin if their user_id has an admin event in the admins table.
    Admin grants are event-sourced via first-class admin events.

    Args:
        peer_shared_id: Public peer ID to check
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if peer is an admin, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get network_id for admin table lookup
    network_row = safedb.query_one(
        "SELECT network_id FROM networks WHERE recorded_by = ? LIMIT 1",
        (recorded_by,)
    )
    if not network_row:
        return False

    network_id = network_row['network_id']

    # Get user_id for this peer_shared_id from peers_shared (user→peer relationship stored there)
    peer_row = safedb.query_one(
        "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, recorded_by)
    )
    if not peer_row or not peer_row['user_id']:
        return False

    user_id = peer_row['user_id']

    # Check if user has an admin event in the admins table (per spec)
    admin_row = safedb.query_one(
        "SELECT 1 FROM admins WHERE user_id = ? AND network_id = ? AND recorded_by = ? LIMIT 1",
        (user_id, network_id, recorded_by)
    )

    return admin_row is not None


def validate(inviter_user_id: str, admins_group_id: str, recorded_by: str, db: Any) -> bool:
    """Validate that inviter has authorization to create invites.

    DEPRECATED: This function is kept for backward compatibility.
    New code should use is_admin() instead.

    Authorization rule:
    - inviter_user_id must be a member of the network's admins group

    Args:
        inviter_user_id: User attempting to create invite (user event ID)
        admins_group_id: The network's admins group ID
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if authorized, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Check if inviter is in admin group
    is_admin_check = safedb.query_one(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ? AND recorded_by = ? LIMIT 1",
        (admins_group_id, inviter_user_id, recorded_by)
    )

    return is_admin_check is not None


def create(peer_id: str, t_ms: int, db: Any, mode: str = 'user', user_id: str | None = None) -> tuple[str, str, dict[str, Any]]:
    """Create an invite event and generate invite link.

    Automatically queries for the inviter's main group, main channel, and peer_shared_id.
    Only admins can create invites (checked via admin event chain).

    Note: Bootstrap invites use create_bootstrap_user_invite() instead, which is
    signed by network_id and doesn't require admin check.

    SECURITY: This function trusts that peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function. This is safe for local-only apps where
    the user controls all peers on the device.

    Args:
        peer_id: Local peer ID of the inviter
        t_ms: Timestamp
        db: Database connection
        mode: 'user' for network join invites, 'peer' for device linking invites
        user_id: Required for mode='peer', target user to link to. Must be None for mode='user'.

    Returns:
        (invite_id, invite_link, invite_data): The stored invite event ID, the invite link, and the invite data dict
    """
    # Validate mode-specific requirements
    if mode == 'user':
        if user_id is not None:
            raise ValueError("mode='user' invites cannot have user_id set")
    elif mode == 'peer':
        if user_id is None:
            raise ValueError("mode='peer' invites must have user_id set")
    else:
        raise ValueError(f"Invalid mode: {mode}. Must be 'user' or 'peer'")
    safedb = create_safe_db(db, recorded_by=peer_id)
    unsafedb = create_unsafe_db(db)

    # Query peer_self to get peer_shared_id (subjective table)
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set in peer_self table")

    peer_shared_id = peer_self_row['peer_shared_id']

    # Get network
    network_row = safedb.query_one(
        "SELECT network_id FROM networks WHERE recorded_by = ? LIMIT 1",
        (peer_id,)
    )
    if not network_row:
        raise ValueError(f"No network found for peer {peer_id}. Cannot create invite.")

    network_id = network_row['network_id']

    # Get all_users group by signature (network-signed group = all_users)
    from events.identity import network as network_module
    all_users_group_id = network_module.get_all_users_group_id(network_id, peer_id, db)

    # Check if inviter is an admin (only admins can create invites)
    if not is_admin(peer_shared_id, peer_id, db):
        raise ValueError(f"Only admins can create invites. Peer {peer_id} is not an admin.")

    # Get inviter's user_id from peers_shared (user→peer relationship stored there)
    peer_row = safedb.query_one(
        "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, peer_id)
    )
    if not peer_row or not peer_row['user_id']:
        raise ValueError(f"User record not found for peer_shared_id {peer_shared_id}. Cannot create invite.")

    inviter_user_id = peer_row['user_id']

    # Per spec: Ongoing invite(mode=user) must include admin_grant that authorizes the signer
    # Look up the admin event that grants admin to this user
    from events.identity import admin as admin_module
    admin_grant_id = admin_module.my_grant(inviter_user_id, network_id, peer_id, db)
    if admin_grant_id:
        log.info(f"invite.create() including admin_grant={admin_grant_id[:20]}... for ongoing invite")
    else:
        # This shouldn't happen if is_admin() passed, but warn just in case
        log.warning(f"invite.create() no admin_grant found for user {inviter_user_id[:20]}...")

    # Get key from all_users group
    group_row = safedb.query_one(
        "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ? LIMIT 1",
        (all_users_group_id, peer_id)
    )
    if not group_row:
        raise ValueError(f"No key found for all_users group {all_users_group_id}. Cannot create invite.")

    key_id = group_row['key_id']

    # Get main channel
    channel_row = safedb.query_one(
        "SELECT channel_id FROM channels WHERE recorded_by = ? AND is_main = 1 LIMIT 1",
        (peer_id,)
    )
    if not channel_row:
        raise ValueError(f"No main channel found for peer {peer_id}. Cannot create invite.")

    channel_id = channel_row['channel_id']

    # Create a group_prekey (generates keypair) then share it via group_prekey_shared
    # This ensures invite_prekey_id is an actual event ID, so dependencies work naturally
    from events.group import group_prekey, group_prekey_shared

    # Create local prekey with keypair
    local_prekey_id, invite_private_key = group_prekey.create(peer_id, t_ms + 1, db)

    # Get the public key from the created prekey for the invite event
    prekey_blob = store.get(local_prekey_id, unsafedb)
    prekey_data = crypto.parse_json(prekey_blob)
    invite_pubkey_b64 = prekey_data['public_key']

    # Create shareable prekey event - its event ID becomes invite_prekey_id
    # Context depends on mode:
    # - mode='user': group context (all_users_group)
    # - mode='peer': user context (user_id being linked to)
    if mode == 'peer':
        # Device linking: context is the user being linked to
        invite_prekey_id = group_prekey_shared.create(
            prekey_id=local_prekey_id,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms + 2,
            db=db,
            user_id=user_id  # User context for device linking
        )
    else:
        # User invite: context is the all_users group
        invite_prekey_id = group_prekey_shared.create(
            prekey_id=local_prekey_id,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms + 2,
            db=db,
            group_id=all_users_group_id,
            key_id=key_id
        )

    # Get inviter's prekey for Bob to send sync requests
    # Query prekey from transit_prekeys table
    inviter_prekey_row = unsafedb.query_one(
        "SELECT transit_prekey_id, public_key FROM transit_prekeys WHERE owner_peer_id = ? ORDER BY created_at DESC LIMIT 1",
        (peer_id,)
    )

    if not inviter_prekey_row:
        raise ValueError(f"No prekey found for inviter {peer_id}. Cannot create invite.")

    inviter_prekey_id = inviter_prekey_row['transit_prekey_id']
    inviter_prekey_public_key = inviter_prekey_row['public_key']  # Raw bytes from DB

    # Get prekey_shared_id from transit_prekeys_shared table
    # Phase 3: Query by recorded_by only (not peer_id) since peer_id may be old peer_shared
    # after Phase 3 isomorphic linking creates new peer_shared_id
    inviter_prekey_shared_row = safedb.query_one(
        "SELECT transit_prekey_shared_id, created_at FROM transit_prekeys_shared WHERE recorded_by = ? ORDER BY created_at DESC LIMIT 1",
        (peer_id,)
    )

    if not inviter_prekey_shared_row:
        raise ValueError(f"No prekey_shared found for inviter {peer_id}. Cannot create invite.")

    inviter_transit_prekey_shared_id = inviter_prekey_shared_row['transit_prekey_shared_id']
    inviter_transit_prekey_shared_created_at = inviter_prekey_shared_row['created_at']

    # Address info (hardcoded for now, would come from address table in production)
    inviter_ip = '127.0.0.1'
    inviter_port = 6100

    # Create minimal invite event (signed by Alice, proves authorization)
    # This event contains group/channel/key metadata that Bob's user event will reference
    # Include inviter's prekey so Bob can send sync requests (projection into prekeys_shared)
    invite_event_data = {
        'type': 'invite',
        'mode': mode,  # NEW - 'user' or 'link'
        'invite_pubkey': invite_pubkey_b64,  # For user proof signature
        'invite_prekey_id': invite_prekey_id,  # Crypto hint for GKS (deterministic hash)
        'network_id': network_id,  # NEW - explicit network reference
        'group_id': all_users_group_id,  # All users group (for adding joiner)
        'channel_id': channel_id,
        'key_id': key_id,
        'inviter_peer_shared_id': peer_shared_id,
        'inviter_user_id': inviter_user_id,  # NEW - for admin validation during projection
        'inviter_transit_prekey_public_key': crypto.b64encode(inviter_prekey_public_key),
        'inviter_transit_prekey_shared_id': inviter_transit_prekey_shared_id,
        'inviter_transit_prekey_shared_created_at': inviter_transit_prekey_shared_created_at,  # For correct created_at in transit_prekeys_shared
        'inviter_transit_prekey_id': inviter_prekey_id,
        'address': inviter_ip,  # For bootstrap connections (stored in invite_accepteds)
        'port': inviter_port,   # For bootstrap connections (stored in invite_accepteds)
        'signed_by': peer_shared_id,
        'created_at': t_ms
    }

    # Per spec: Ongoing invite(mode=user) must include admin_grant that authorizes the signer
    if admin_grant_id:
        invite_event_data['admin_grant'] = admin_grant_id

    # Add mode-specific fields
    if mode == 'peer':
        invite_event_data['user_id'] = user_id  # Target user for device linking

    # Sign the invite event with inviter's peer private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_invite_event = crypto.sign_event(invite_event_data, private_key)

    # Canonicalize and store the invite event (with recorded wrapper for reprojection)
    # store.event() will automatically project the invite, restoring keys from event data
    invite_blob = crypto.canonicalize_json(signed_invite_event)
    invite_id = store.event(invite_blob, peer_id, t_ms, db)

    # Create group_key_shared sealed to invite proof prekey
    # The create_for_invite function will extract the prekey from the invite event
    from events.group import group_key_shared

    # Share all_users group key
    group_key_shared_id = group_key_shared.create_for_invite(
        key_id=key_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        invite_id=invite_id,  # Pass invite_id to extract prekey from stored invite
        t_ms=t_ms + 3,
        db=db
    )

    log.info(f"invite.create() created group_key_shared {group_key_shared_id[:20]}... for all_users group key")

    # Share admins group key (so all users can see who admins are)
    # Get admins group - find by querying groups with network_id that are NOT signed by network
    # (admins group is peer-signed, not network-signed like all_users)
    admin_key_id = None
    admins_group_row = safedb.query_one(
        """SELECT group_id, key_id FROM groups
           WHERE network_id = ? AND signed_by != ? AND recorded_by = ?
           AND name LIKE '% - Admins'
           LIMIT 1""",
        (network_id, network_id, peer_id)
    )

    if admins_group_row:
        admins_group_id = admins_group_row['group_id']
        admin_key_id = admins_group_row['key_id']

        # Share admin group key
        admin_key_shared_id = group_key_shared.create_for_invite(
            key_id=admin_key_id,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            invite_id=invite_id,
            t_ms=t_ms + 4,
            db=db
        )
        log.info(f"invite.create() created group_key_shared {admin_key_shared_id[:20]}... for admins group key")

    # For mode='peer', share keys for ALL groups this user is a member of
    # This ensures the new device can decrypt all groups the user belongs to
    if mode == 'peer':
        log.info(f"invite.create() mode='peer' - sharing keys for user {user_id[:20]}...'s group memberships")

        # Query groups that THIS USER is a member of (not all groups peer knows about)
        # Join group_members with groups to get both group_id and key_id
        group_rows = safedb.query(
            """SELECT DISTINCT g.group_id, g.key_id
               FROM group_members gm
               JOIN groups g ON gm.group_id = g.group_id AND gm.recorded_by = g.recorded_by
               WHERE gm.user_id = ? AND gm.recorded_by = ?
               ORDER BY g.group_id""",
            (user_id, peer_id)
        )

        log.info(f"invite.create() found {len(group_rows)} groups for user {user_id[:20]}...")

        ts = t_ms + 5
        for group_row in group_rows:
            group_id = group_row['group_id']
            key_id_for_group = group_row['key_id']

            # Skip if we already created it above (all_users or admins)
            if key_id_for_group == key_id or (admin_key_id and key_id_for_group == admin_key_id):
                continue

            # Create group_key_shared sealed to invite_prekey
            group_key_shared_id = group_key_shared.create_for_invite(
                key_id=key_id_for_group,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                invite_id=invite_id,
                t_ms=ts,
                db=db
            )
            ts += 1
            log.info(f"invite.create() created group_key_shared {group_key_shared_id[:20]}... for group {group_id[:20]}...")

    # Get inviter's peer_shared blob to include in invite link
    # This allows Bob to immediately have Alice in his peers_shared table upon joining
    inviter_peer_shared_blob = store.get(peer_shared_id, unsafedb)
    if not inviter_peer_shared_blob:
        raise ValueError(f"Inviter's peer_shared blob not found: {peer_shared_id}. Cannot create invite.")

    # Build invite link with invite blob + secrets
    # Group/channel/key metadata is now in the signed blob (not plaintext)
    import base64
    invite_blob_b64 = base64.urlsafe_b64encode(invite_blob).decode().rstrip('=')
    inviter_peer_shared_blob_b64 = base64.urlsafe_b64encode(inviter_peer_shared_blob).decode().rstrip('=')

    invite_link_data = {
        'invite_blob': invite_blob_b64,  # Signed invite event (contains group/channel/key + invite prekey_id)
        'invite_id': invite_id,  # Event ID for reference
        'invite_prekey_id': invite_prekey_id,  # Crypto hint (where Bob stores the key)
        'invite_private_key': crypto.b64encode(invite_private_key),  # Key material for GKS decryption + proof
        'inviter_peer_shared_id': peer_shared_id,  # Alice's peer_shared_id for Bob to send sync requests
        'inviter_peer_shared_blob': inviter_peer_shared_blob_b64,  # Alice's peer_shared blob for immediate projection
        'network_id': network_id,  # For joiner to know which network they're joining
        'ip': inviter_ip,
        'port': inviter_port,
    }

    # For mode='peer', also include the existing user blob (for device linking)
    if mode == 'peer' and user_id:
        existing_user_blob = store.get(user_id, unsafedb)
        if existing_user_blob:
            existing_user_blob_b64 = base64.urlsafe_b64encode(existing_user_blob).decode().rstrip('=')
            invite_link_data['existing_user_blob'] = existing_user_blob_b64
            log.info(f"invite.create() added existing_user_blob for mode='link'")

    # Encode invite link as base64-urlsafe JSON
    import base64
    invite_json = json.dumps(invite_link_data, separators=(',', ':'), sort_keys=True)
    invite_code = base64.urlsafe_b64encode(invite_json.encode()).decode().rstrip('=')

    # Use different URL prefix based on mode
    # Note: URL prefix "link" is kept for backward compatibility with mode='peer'
    url_prefix = "link" if mode == "peer" else "invite"
    invite_link = f"quiet://{url_prefix}/{invite_code}"

    log.info(f"invite.create() invite link created (mode={mode}) with invite_prekey_id={invite_prekey_id[:20]}...")

    return (invite_id, invite_link, invite_link_data)


def project_event(invite_id: str, recorded_by: str, recorded_at: int, db: Any,
            skip_admin_check: bool = False) -> str | None:
    """Project invite event into invites table.

    Uses pure functional projector.
    Note: Keeps side effect for projecting inviter's transit_prekey.

    Args:
        skip_admin_check: If True, skip admin validation. Used for invites
            received out-of-band (invite links) where the joiner trusts the invite.
    """
    log.debug(f"invite.project_event() projecting invite_id={invite_id[:20]}...")

    from projection import resolve

    safedb = create_safe_db(db, recorded_by=recorded_by)

    input_dict = resolve("invite", invite_id, recorded_by, recorded_at, db)
    if not input_dict:
        log.warning(f"invite.project_event() resolve failed for {invite_id[:20]}...")
        return None

    result = project(input_dict)

    if result.blocked or not result.valid:
        log.warning(f"invite.project_event() failed: {result.reason}")
        return None

    # Apply invites (INSERT OR IGNORE)
    for row in result.tables.get("invites", []):
        safedb.execute(
            """INSERT OR IGNORE INTO invites
               (invite_id, invite_pubkey, group_id, inviter_id, mode, user_id, created_at, recorded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["invite_id"], row["invite_pubkey"], row["group_id"], row["inviter_id"],
             row["mode"], row["user_id"], row["created_at"], row["recorded_by"])
        )

    # Apply valid_events (INSERT OR IGNORE)
    for row in result.tables.get("valid_events", []):
        safedb.execute(
            "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
            (row["event_id"], row["recorded_by"])
        )

    log.info(f"invite.project() stored invite {invite_id[:20]}...")

    # Side effect: Project inviter's prekey into transit_prekeys_shared (for Bob to send sync requests to Alice)
    # Always project for anyone who receives the invite (INSERT OR IGNORE handles duplicates)
    event_data = input_dict["event_data"]
    if all(k in event_data for k in ['inviter_transit_prekey_public_key', 'inviter_peer_shared_id',
                                      'inviter_transit_prekey_shared_id', 'inviter_transit_prekey_id']):
        inviter_prekey_public_key_bytes = crypto.b64decode(event_data['inviter_transit_prekey_public_key'])
        inviter_peer_shared_id = event_data['inviter_peer_shared_id']

        # Use inviter_transit_prekey_shared_created_at if available, otherwise fall back to invite's created_at
        # (for backwards compatibility with old invites that don't have this field)
        prekey_created_at = event_data.get('inviter_transit_prekey_shared_created_at', event_data['created_at'])

        log.info(f"invite.project() projecting inviter's prekey for {recorded_by[:20]}... to contact {inviter_peer_shared_id[:20]}...")
        safedb.execute(
            "INSERT OR IGNORE INTO transit_prekeys_shared (transit_prekey_shared_id, transit_prekey_id, peer_id, public_key, created_at, recorded_by) VALUES (?, ?, ?, ?, ?, ?)",
            (event_data['inviter_transit_prekey_shared_id'], event_data['inviter_transit_prekey_id'], inviter_peer_shared_id, inviter_prekey_public_key_bytes, prekey_created_at, recorded_by)
        )

    return invite_id


def create_peer_invite(
    user_id: str,
    signer_id: str,
    signer_private_key: bytes,
    peer_id: str,
    t_ms: int,
    db: Any
) -> tuple[str, bytes, bytes]:
    """Create an invite(mode=peer) for linking a peer to a user.

    Phase 3: Uniform peer linking - first peer and later peers use same flow.

    For first peer: signer_id=user_id, signer_private_key=user_private_key
    For later peers: signer_id=peer_shared_id, signer_private_key=peer_private_key

    Args:
        user_id: The user this peer will be linked to
        signer_id: Who signs this invite (user_id for first peer, peer_shared_id for later)
        signer_private_key: Private key of the signer
        peer_id: Local peer ID (for recording)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (invite_id, invite_private_key, invite_pubkey): The invite ID and keys for peer_shared signing
    """
    # Generate keypair for this peer invite
    invite_private_key, invite_pubkey = crypto.generate_keypair()

    # Create peer invite event
    event_data = {
        'type': 'invite',
        'mode': 'peer',
        'user_id': user_id,  # Which user this peer will link to
        'invite_pubkey': crypto.b64encode(invite_pubkey),
        'signed_by': signer_id,  # user_id for first peer, peer_shared_id for later
        'created_at': t_ms
    }

    # Sign with signer's private key
    signed_event = crypto.sign_event(event_data, signer_private_key)

    # Store the invite event
    blob = crypto.canonicalize_json(signed_event)
    invite_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"create_peer_invite() created invite(mode=peer) {invite_id[:20]}... signed_by={signer_id[:20]}...")

    return invite_id, invite_private_key, invite_pubkey


def create_bootstrap_user_invite(
    network_id: str,
    network_private_key: bytes,
    group_id: str,
    channel_id: str,
    key_id: str,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any
) -> tuple[str, bytes, bytes]:
    """Create an invite(mode=user) for bootstrap - signed by network key.

    Bootstrap user invite is signed by network_id (not peer_shared_id).
    This is used when creating the first user in a new network.
    Admin privileges are granted via admin event after join, not via invite.

    Args:
        network_id: The network this invite is for
        network_private_key: Network's private key for signing
        group_id: All-users group ID
        channel_id: Default channel ID
        key_id: Group key ID
        peer_id: Local peer ID (for recording)
        peer_shared_id: Public peer ID of the bootstrap peer (for inviter_peer_shared_id)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (invite_id, invite_private_key, invite_pubkey): The invite ID and keys for user signing
    """
    # Generate keypair for this user invite
    invite_private_key, invite_pubkey = crypto.generate_keypair()

    # Create bootstrap user invite event
    # For bootstrap, the creator IS the inviter (self-invite)
    event_data = {
        'type': 'invite',
        'mode': 'user',
        'network_id': network_id,
        'group_id': group_id,
        'channel_id': channel_id,
        'key_id': key_id,
        'invite_pubkey': crypto.b64encode(invite_pubkey),
        'signed_by': network_id,  # Bootstrap: signed by network key
        'inviter_peer_shared_id': peer_shared_id,  # For bootstrap, inviter is self
        'created_at': t_ms
    }

    # Sign with network's private key
    signed_event = crypto.sign_event(event_data, network_private_key)

    # Store the invite event
    blob = crypto.canonicalize_json(signed_event)
    invite_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"create_bootstrap_user_invite() created invite(mode=user) {invite_id[:20]}... signed_by=network_id")

    return invite_id, invite_private_key, invite_pubkey


def accept(peer_id: str, invite_link: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Accept an invite link (mode-agnostic, link-specific logic).

    Centralized invite acceptance that handles both network join (mode='user')
    and device linking (mode='peer') flows. Mode is encoded in the URL prefix.

    Args:
        peer_id: Receiving peer
        invite_link: Full URL (quiet://invite/{code} or quiet://link/{code})
        t_ms: Timestamp
        db: Database connection

    Returns:
        Dict with:
        - mode: 'user' (network join) or 'peer' (device linking)
        - invite_id: Projected invite ID
        - user_id: (For mode='peer' only) Target user to link to
        - inviter_peer_shared_id: Inviter's peer_shared ID (projected locally)
        - invite_prekey_id: Crypto hint for GKS decryption
        - invite_private_key: For proof signing
        - Other invite/inviter metadata

    Steps:
    1. Parse invite link - extract mode from URL prefix
    2. Store invite event blob and project to invites table
    3. Store inviter's peer_shared blob and project (makes inviter public key available)
    4. Create invite_accepted event (event-source secrets for GKS)
    5. Project invite_accepted to invite_accepteds table
    6. Return mode + data for caller to decide flow

    NOTE: invite_accepted projects to invite_accepteds table (connection/sync metadata),
    not peers_shared. peers_shared stays clean with just peer identity info.
    """
    import base64

    log.info(f"invite.accept() processing invite_link for peer {peer_id[:20]}...")

    # Parse invite link - extract mode from URL prefix
    if invite_link.startswith("quiet://invite/"):
        mode = 'user'
        code = invite_link.replace("quiet://invite/", "")
    elif invite_link.startswith("quiet://link/"):
        mode = 'peer'
        code = invite_link.replace("quiet://link/", "")
    else:
        raise ValueError(f"Invalid invite link format: {invite_link}")

    log.info(f"invite.accept() parsed invite_link with mode={mode}")

    # Decode base64-urlsafe JSON
    try:
        padding = '=' * (4 - len(code) % 4)
        link_json = base64.urlsafe_b64decode(code + padding).decode()
        link_data = json.loads(link_json)
    except Exception as e:
        raise ValueError(f"Failed to parse invite link: {e}")

    # Extract link data
    invite_blob_b64 = link_data['invite_blob']
    invite_id = link_data['invite_id']
    invite_prekey_id = link_data['invite_prekey_id']
    invite_private_key_b64 = link_data['invite_private_key']
    inviter_peer_shared_id = link_data['inviter_peer_shared_id']
    inviter_peer_shared_blob_b64 = link_data['inviter_peer_shared_blob']

    # Decode blobs and keys
    invite_blob = base64.urlsafe_b64decode(invite_blob_b64 + '=' * (4 - len(invite_blob_b64) % 4))
    inviter_peer_shared_blob = base64.urlsafe_b64decode(inviter_peer_shared_blob_b64 + '=' * (4 - len(inviter_peer_shared_blob_b64) % 4))
    invite_private_key = crypto.b64decode(invite_private_key_b64)

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Step 1: Store invite event blob
    invite_stored_id = store.event(invite_blob, peer_id, t_ms, db)
    if invite_stored_id != invite_id:
        # Event already stored (same hash), continue
        pass
    log.info(f"invite.accept() stored invite_id={invite_id[:20]}...")

    # Step 2: Store inviter's peer_shared blob and project it
    # This makes the inviter's public key available locally
    # NOTE: Must happen BEFORE invite projection - for mode='peer' invites signed by
    # peer_shared_id, signature verification needs the signer's public key
    inviter_stored_id = store.event(inviter_peer_shared_blob, peer_id, t_ms, db)
    if inviter_stored_id != inviter_peer_shared_id:
        # Event already stored (same hash), continue
        pass

    from events.identity import peer_shared as peer_shared_module
    peer_shared_module.project_event(inviter_peer_shared_id, peer_id, t_ms, db)
    log.info(f"invite.accept() projected inviter's peer_shared {inviter_peer_shared_id[:20]}...")

    # Step 3: Project invite event to invites table
    # Now that inviter's peer_shared is available, signature can be verified
    project_event(invite_id, peer_id, t_ms, db, skip_admin_check=True)
    log.info(f"invite.accept() projected invite to invites table")

    # Step 4: Create invite_accepted event (event-sources invite secrets for GKS)
    from events.identity import invite_accepted as invite_accepted_module
    invite_accepted_id = invite_accepted_module.create(
        invite_id=invite_id,
        invite_prekey_id=invite_prekey_id,
        invite_private_key=invite_private_key,
        peer_id=peer_id,
        t_ms=t_ms + 1,
        db=db
    )
    log.info(f"invite.accept() created invite_accepted_id={invite_accepted_id[:20]}...")

    # Step 5: Project invite_accepted (projects secrets to group_prekeys, projects invite_accepteds table)
    invite_accepted_module.project_event(invite_accepted_id, peer_id, t_ms + 1, db)
    log.info(f"invite.accept() projected invite_accepted")

    # For mode='peer', also store existing_user_blob if present (for offline bootstrap)
    user_id = None
    if mode == 'peer' and 'existing_user_blob' in link_data:
        existing_user_blob_b64 = link_data['existing_user_blob']
        existing_user_blob = base64.urlsafe_b64decode(existing_user_blob_b64 + '=' * (4 - len(existing_user_blob_b64) % 4))

        # Store the existing user blob (makes user available locally for offline bootstrap)
        # Note: Don't project it here - projection happens naturally via event dependencies
        user_stored_id = store.event(existing_user_blob, peer_id, t_ms, db)
        log.info(f"invite.accept() stored existing user blob for device linking (bootstrap)")

    # Extract user_id from invite event (for mode='peer', this contains the target user)
    invite_event = crypto.parse_json(invite_blob)
    user_id = invite_event.get('user_id')

    # Build return dict
    result = {
        'mode': mode,
        'invite_id': invite_id,
        'invite_prekey_id': invite_prekey_id,
        'invite_private_key': invite_private_key,
        'inviter_peer_shared_id': inviter_peer_shared_id,
    }

    if user_id:
        result['user_id'] = user_id

    log.info(f"invite.accept() completed for mode={mode}, peer={peer_id[:20]}...")
    return result
