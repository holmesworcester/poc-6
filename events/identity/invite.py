"""Invite event type (shareable, encrypted)."""
from typing import Any
import secrets
import json
import logging
import crypto
import store
from events.transit import transit_key
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def validate(inviter_user_id: str, admins_group_id: str, recorded_by: str, db: Any) -> bool:
    """Validate that inviter has authorization to create invites.

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
    is_admin = safedb.query_one(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ? AND recorded_by = ? LIMIT 1",
        (admins_group_id, inviter_user_id, recorded_by)
    )

    return is_admin is not None


def create(peer_id: str, t_ms: int, db: Any, mode: str = 'user', user_id: str | None = None) -> tuple[str, str, dict[str, Any]]:
    """Create an invite event and generate invite link.

    Automatically queries for the inviter's main group, main channel, and peer_shared_id.

    SECURITY: This function trusts that peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function. This is safe for local-only apps where
    the user controls all peers on the device.

    Args:
        peer_id: Local peer ID of the inviter
        t_ms: Timestamp
        db: Database connection
        mode: 'user' for network join invites, 'link' for device linking invites
        user_id: Required for mode='link', target user to link to. Must be None for mode='user'.

    Returns:
        (invite_id, invite_link, invite_data): The stored invite event ID, the invite link, and the invite data dict
    """
    # Validate mode-specific requirements
    if mode == 'user':
        if user_id is not None:
            raise ValueError("mode='user' invites cannot have user_id set")
    elif mode == 'link':
        if user_id is None:
            raise ValueError("mode='link' invites must have user_id set")
    else:
        raise ValueError(f"Invalid mode: {mode}. Must be 'user' or 'link'")
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

    # Query for network (which contains all_users group and other metadata)

    # Get network
    network_row = safedb.query_one(
        "SELECT network_id, all_users_group_id, admins_group_id FROM networks WHERE recorded_by = ? LIMIT 1",
        (peer_id,)
    )
    if not network_row:
        raise ValueError(f"No network found for peer {peer_id}. Cannot create invite.")

    network_id = network_row['network_id']
    all_users_group_id = network_row['all_users_group_id']
    admins_group_id = network_row['admins_group_id']

    # Check if inviter is an admin (only admins can create invites)
    # First, get the inviter's user_id (event ID, not peer_shared_id)
    inviter_user_row = safedb.query_one(
        "SELECT user_id FROM users WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, peer_id)
    )
    if not inviter_user_row:
        raise ValueError(f"User record not found for peer {peer_id}. Cannot create invite.")

    inviter_user_id = inviter_user_row['user_id']

    # Check authorization using shared validate() function
    if not validate(inviter_user_id, admins_group_id, peer_id, db):
        raise ValueError(f"Only admins can create invites. Peer {peer_id} is not an admin.")

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

    # Generate Ed25519 keypair for invite proof + GKS decryption
    invite_private_key, invite_public_key = crypto.generate_keypair()
    invite_pubkey_b64 = crypto.b64encode(invite_public_key)

    # Generate deterministic prekey ID from public key hash
    # This serves as the crypto hint for group_key_shared decryption
    invite_prekey_id = crypto.b64encode(crypto.hash(invite_public_key)[:16])

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
    inviter_prekey_shared_row = safedb.query_one(
        "SELECT transit_prekey_shared_id, created_at FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ? ORDER BY created_at DESC LIMIT 1",
        (peer_shared_id, peer_id)
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
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

    # Add mode-specific fields
    if mode == 'link':
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
    # Get admins group ID from network
    from events.identity import network
    network_row_for_admins = safedb.query_one(
        "SELECT network_id, admins_group_id FROM networks WHERE recorded_by = ? LIMIT 1",
        (peer_id,)
    )
    if network_row_for_admins and network_row_for_admins['admins_group_id']:
        admins_group_id = network_row_for_admins['admins_group_id']

        # Get admin group key
        admin_group_row = safedb.query_one(
            "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ? LIMIT 1",
            (admins_group_id, peer_id)
        )

        if admin_group_row:
            admin_key_id = admin_group_row['key_id']

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
        'ip': inviter_ip,
        'port': inviter_port,
    }

    # Encode invite link as base64-urlsafe JSON
    import base64
    invite_json = json.dumps(invite_link_data, separators=(',', ':'), sort_keys=True)
    invite_code = base64.urlsafe_b64encode(invite_json.encode()).decode().rstrip('=')
    invite_link = f"quiet://invite/{invite_code}"

    log.info(f"invite.create() invite link created with invite_prekey_id={invite_prekey_id[:20]}...")

    return (invite_id, invite_link, invite_link_data)


def project(invite_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project minimal invite event into invites table."""
    # Create db wrappers first (consistent with other projectors)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(invite_id, unsafedb)
    if not blob:
        return None

    # Parse JSON (plaintext, no unwrap needed)
    event_data = crypto.parse_json(blob)

    created_by = event_data['created_by']

    # Check if this is a bootstrap invite (first join via URL)
    # Bootstrap invites are processed before peer has any networks, and before invite_accepted is created
    # If peer has no networks, this is a bootstrap - skip validation (root of trust from URL)
    peer_networks = safedb.query_one(
        "SELECT 1 FROM networks WHERE recorded_by = ? LIMIT 1",
        (recorded_by,)
    )

    is_bootstrap = (peer_networks is None)

    # If not bootstrap, this came via sync or is a second invite - validate it
    if not is_bootstrap:
        log.info(f"invite.project() sync-sourced invite, validating...")

        # 1. Verify creator (created_by) exists
        from events.identity import peer_shared
        creator_public_key = peer_shared.get_public_key(created_by, recorded_by, db)
        if not creator_public_key:
            log.warning(f"invite.project() creator not found: {created_by[:20]}...")
            return None

        # 2. Verify signature
        if not crypto.verify_event(event_data, creator_public_key):
            log.warning(f"invite.project() signature verification FAILED for invite {invite_id[:20]}...")
            return None

        # 3. Verify network_id matches peer's network
        invite_network_id = event_data.get('network_id')
        if invite_network_id:
            peer_network = safedb.query_one(
                "SELECT admins_group_id FROM networks WHERE network_id = ? AND recorded_by = ? LIMIT 1",
                (invite_network_id, recorded_by)
            )
            if not peer_network:
                log.warning(f"invite.project() network mismatch: invite for {invite_network_id[:20]}... but peer not in that network")
                return None

            # 4. Verify inviter is an admin using shared validate() function
            inviter_user_id = event_data.get('inviter_user_id')
            if inviter_user_id and peer_network['admins_group_id']:
                admins_group_id = peer_network['admins_group_id']
                if not validate(inviter_user_id, admins_group_id, recorded_by, db):
                    log.warning(f"invite.project() authorization FAILED: inviter {inviter_user_id[:20]}... is not an admin")
                    return None
            else:
                # If inviter_user_id is missing (old invite format), reject for safety
                log.warning(f"invite.project() missing inviter_user_id in invite event - rejecting for security")
                return None

        log.info(f"invite.project() validation passed for sync-sourced invite")
    else:
        log.info(f"invite.project() bootstrap invite (first join via URL), skipping validation")

    # Insert into invites table
    mode = event_data.get('mode', 'user')  # Default to 'user' for backward compatibility
    user_id = event_data.get('user_id')  # None for mode='user', set for mode='link'

    safedb.execute(
        """INSERT OR IGNORE INTO invites
           (invite_id, invite_pubkey, group_id, inviter_id, mode, user_id, created_at, recorded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            invite_id,
            event_data['invite_pubkey'],
            event_data['group_id'],
            created_by,  # Use created_by (inviter's peer_shared_id)
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
