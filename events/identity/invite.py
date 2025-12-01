"""Invite event type (shareable, encrypted)."""

# Registry metadata
EVENT_TYPE = 'invite'
SHAREABLE = True  # Invites sync to enable network membership
EPHEMERAL = False
PROJECTION_TABLE = ('invites', 'invite_id')

from typing import Any
import secrets
import json
import logging
import crypto
import store
from events.network import transit_key
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


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
    # The invite_prekey_id is the local group_prekey_id (for crypto hint lookup)
    # The group_prekey_shared is created for sync/sharing but its ID is not used as hint
    from events.group import group_prekey, group_prekey_shared

    # Create local prekey with keypair
    local_prekey_id, invite_private_key = group_prekey.create(peer_id, t_ms + 1, db)

    # Get the public key from the created prekey for the invite event
    prekey_blob = store.get(local_prekey_id, unsafedb)
    prekey_data = crypto.parse_json(prekey_blob)
    invite_pubkey_b64 = prekey_data['public_key']

    # Create shareable prekey event for sync
    # Context depends on mode:
    # - mode='user': group context (all_users_group)
    # - mode='peer': user context (user_id being linked to)
    if mode == 'peer':
        # Device linking: context is the user being linked to
        group_prekey_shared.create(
            prekey_id=local_prekey_id,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms + 2,
            db=db,
            user_id=user_id  # User context for device linking
        )
    else:
        # User invite: context is the all_users group
        group_prekey_shared.create(
            prekey_id=local_prekey_id,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms + 2,
            db=db,
            group_id=all_users_group_id,
            key_id=key_id
        )

    # Use local_prekey_id as invite_prekey_id (consistent with transit_prekey pattern)
    # This is the ID the recipient uses to look up the private key for decryption
    invite_prekey_id = local_prekey_id

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


def project(invite_id: str, recorded_by: str, recorded_at: int, db: Any,
            skip_admin_check: bool = False) -> str | None:
    """Project invite event into invites table.

    Phase 4: Supports polymorphic signed_by:
    - signed_by=network_id (bootstrap): verify with network pubkey
    - signed_by=peer_shared_id (ongoing): verify with peer_shared pubkey
    - Legacy (no signed_by): verify with created_by peer_shared pubkey

    Args:
        skip_admin_check: If True, skip admin validation. Used for invites
            received out-of-band (invite links) where the joiner trusts the invite.
    """
    # Create db wrappers first (consistent with other projectors)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(invite_id, unsafedb)
    if not blob:
        return None

    # Parse JSON (plaintext, no unwrap needed)
    event_data = crypto.parse_json(blob)

    # Phase 4: Determine verification mode based on signed_by
    signed_by = event_data.get('signed_by')
    mode = event_data.get('mode', 'user')
    # Extract signer early for INSERT
    # For Phase 4 bootstrap invites with signed_by=network_id, use inviter_peer_shared_id or first_peer
    # For ongoing invites, signed_by IS the inviter's peer_shared_id
    inviter_id = event_data.get('inviter_peer_shared_id') or event_data.get('signed_by') or event_data.get('first_peer', '')

    log.info(f"invite.project() validating invite mode={mode} signed_by={signed_by[:20] if signed_by else 'None'}...")

    if signed_by:
        # New Phase 4 flow: polymorphic signed_by
        network_id = event_data.get('network_id')

        if signed_by == network_id:
            # Bootstrap invite: signed by network key
            # Get network pubkey from networks table or store blob
            network_pubkey = None

            network_row = safedb.query_one(
                "SELECT network_pubkey FROM networks WHERE network_id = ? AND recorded_by = ? LIMIT 1",
                (network_id, recorded_by)
            )
            if network_row and network_row.get('network_pubkey'):
                network_pubkey = crypto.b64decode(network_row['network_pubkey'])
            else:
                # Try store blob (bootstrap case)
                network_blob = store.get(network_id, unsafedb)
                if network_blob:
                    network_data = crypto.parse_json(network_blob)
                    network_pubkey_b64 = network_data.get('network_pubkey')
                    if network_pubkey_b64:
                        network_pubkey = crypto.b64decode(network_pubkey_b64)
                        log.info(f"invite.project() got network_pubkey from store blob")

            if not network_pubkey:
                log.warning(f"invite.project() network_id={network_id[:20]}... not available yet")
                return None

            if not crypto.verify_event(event_data, network_pubkey):
                log.warning(f"invite.project() signature verification FAILED using network_pubkey")
                return None

            log.info(f"invite.project() verified bootstrap invite with network_pubkey")

        else:
            # Ongoing invite: signed by peer_shared or user
            # Try peer_shared first, then user
            signer_pubkey = None

            # Try peer_shared
            from events.identity import peer_shared
            try:
                signer_pubkey = peer_shared.get_public_key(signed_by, recorded_by, db)
                log.info(f"invite.project() using peer_shared pubkey for signer {signed_by[:20]}...")
            except ValueError:
                pass

            # Try user (for mode=peer signed_by=user_id)
            if not signer_pubkey:
                user_row = safedb.query_one(
                    "SELECT user_pubkey FROM users WHERE user_id = ? AND recorded_by = ? LIMIT 1",
                    (signed_by, recorded_by)
                )
                if user_row and user_row.get('user_pubkey'):
                    signer_pubkey = crypto.b64decode(user_row['user_pubkey'])
                    log.info(f"invite.project() using user_pubkey for signer {signed_by[:20]}...")

            if not signer_pubkey:
                log.warning(f"invite.project() signer {signed_by[:20]}... not available yet")
                return None

            if not crypto.verify_event(event_data, signer_pubkey):
                log.warning(f"invite.project() signature verification FAILED for signed_by={signed_by[:20]}...")
                return None

            log.info(f"invite.project() verified ongoing invite with signer pubkey")

            # Per spec: Validate admin_grant chain for ongoing invite(mode=user)
            # The signer must be an admin, authorized by admin_grant
            admin_grant = event_data.get('admin_grant')
            if admin_grant:
                # Verify admin_grant references an admin event for the signer's user
                # Get signer's user_id from peers_shared (user→peer relationship stored there)
                signer_user_row = safedb.query_one(
                    "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
                    (signed_by, recorded_by)
                )
                if signer_user_row:
                    signer_user_id = signer_user_row['user_id']
                    grant_row = safedb.query_one(
                        "SELECT user_id FROM admins WHERE admin_id = ? AND recorded_by = ?",
                        (admin_grant, recorded_by)
                    )
                    if grant_row and grant_row['user_id'] == signer_user_id:
                        log.info(f"invite.project() admin_grant chain verified for signer {signed_by[:20]}...")
                    else:
                        log.warning(f"invite.project() admin_grant {admin_grant[:20]}... does not authorize signer {signed_by[:20]}...")
                        # Note: Don't reject yet for backward compatibility - just warn
                        # TODO: Make this rejection mandatory once migration is complete
            else:
                # Legacy invite without admin_grant - allow for backward compatibility
                log.info(f"invite.project() no admin_grant in ongoing invite (legacy)")

    else:
        # Legacy flow: no signed_by, use inviter_id as signer
        # Phase 9: This path handles legacy events that used created_by instead of signed_by
        legacy_signer = event_data.get('created_by') or inviter_id
        if not legacy_signer:
            log.warning(f"invite.project() missing both signed_by and created_by")
            return None

        from events.identity import peer_shared
        try:
            creator_public_key = peer_shared.get_public_key(legacy_signer, recorded_by, db)
        except ValueError:
            log.warning(f"invite.project() signer not found: {legacy_signer[:20]}...")
            return None

        if not crypto.verify_event(event_data, creator_public_key):
            log.warning(f"invite.project() signature verification FAILED for invite {invite_id[:20]}...")
            return None

        log.info(f"invite.project() verified legacy invite with signer pubkey")

        # Admin validation using admin events table (not legacy group_members)
        # created_by is the inviter's peer_shared_id
        # Skip admin check for out-of-band invites (invite links) where the joiner trusts the invite
        created_by = event_data.get('created_by') or inviter_id
        if not skip_admin_check and not is_admin(created_by, recorded_by, db):
            log.warning(f"invite.project() authorization FAILED: inviter {created_by[:20]}... is not an admin")
            return None

    log.info(f"invite.project() validation passed")

    # Insert into invites table
    mode = event_data.get('mode', 'user')  # Default to 'user' for backward compatibility
    user_id = event_data.get('user_id')  # None for mode='user', set for mode='link' and mode='peer'
    group_id = event_data.get('group_id')  # None for mode='peer' (peer invites don't have group context)

    safedb.execute(
        """INSERT OR IGNORE INTO invites
           (invite_id, invite_pubkey, group_id, inviter_id, mode, user_id, created_at, recorded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            invite_id,
            event_data['invite_pubkey'],
            group_id,  # May be None for mode='peer'
            inviter_id,  # Use inviter_id (inviter's peer_shared_id)
            mode,
            user_id,
            event_data['created_at'],
            recorded_by
        )
    )

    # Project inviter's prekey into transit_prekeys_shared (for Bob to send sync requests to Alice)
    # Always project for anyone who receives the invite (INSERT OR IGNORE handles duplicates)
    if 'inviter_transit_prekey_public_key' in event_data and 'inviter_peer_shared_id' in event_data and 'inviter_transit_prekey_shared_id' in event_data and 'inviter_transit_prekey_id' in event_data:
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

    # Mark invite as valid for this peer (required for invite_accepted dependencies)
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_id, recorded_by)
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

    # Step 2: Project invite event to invites table
    project(invite_id, peer_id, t_ms, db, skip_admin_check=True)
    log.info(f"invite.accept() projected invite to invites table")

    # Step 3: Store inviter's peer_shared blob and project it
    # This makes the inviter's public key available locally
    inviter_stored_id = store.event(inviter_peer_shared_blob, peer_id, t_ms, db)
    if inviter_stored_id != inviter_peer_shared_id:
        # Event already stored (same hash), continue
        pass

    from events.identity import peer_shared as peer_shared_module
    peer_shared_module.project(inviter_peer_shared_id, peer_id, t_ms, db)
    log.info(f"invite.accept() projected inviter's peer_shared {inviter_peer_shared_id[:20]}...")

    # Step 4: Create invite_accepted event (event-sources invite secrets)
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
    invite_accepted_module.project(invite_accepted_id, peer_id, t_ms + 1, db)
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
