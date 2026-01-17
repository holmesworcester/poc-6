"""Invite event type (shareable, encrypted)."""

# Registry metadata
EVENT_TYPE = 'invite'
SHAREABLE = True  # Invites sync to enable network membership
EPHEMERAL = False
PROJECTION_TABLE = ('invites', 'invite_id')

from typing import Any
import json
import logging
from core import crypto
from core import store
from events.identity import peer, peer_shared, network
from events.content import channel
from events.group import group
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# v2 event specification - polymorphic signer (network, peer_shared, or user)
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {
        'admin_grant': {
            'source': 'table',
            'table': 'admins',
            'key': 'admin_id',
            'key_from': 'admin_grant',
            'fields': ['admin_id', 'user_id'],
            'required_if_present': True,
        },
    },
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for invite events.

    Invites can be signed by network (bootstrap) or peer_shared (ongoing).
    The resolver handles signature verification. This projector just writes
    the invite to the invites table.

    Writes to: invites
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'invite':
        return ProjectorResult(writes=tuple(), valid_event=False)

    signed_by = event_data.get('signed_by')
    if not signed_by:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Extract fields
    invite_pubkey = event_data.get('invite_pubkey')
    if not invite_pubkey:
        return ProjectorResult(writes=tuple(), valid_event=False)

    mode = event_data.get('mode', 'user')
    network_id = event_data.get('network_id')
    group_id = event_data.get('group_id')
    user_id = event_data.get('user_id')  # For mode='peer'
    created_at = event_data.get('created_at')

    # Determine inviter_id from signed_by
    # For ongoing invites, signed_by IS the inviter's peer_shared_id
    # For bootstrap invites signed by network_id, use inviter_peer_shared_id if available
    inviter_id = signed_by
    if signed_by == network_id:
        # Bootstrap invite signed by network - use inviter_peer_shared_id if available
        inviter_id = event_data.get('inviter_peer_shared_id') or signed_by

    # Validate admin_grant chain for ongoing invites (mode=user) if present
    signer = ctx.signer or {}
    admin_grant = event_data.get('admin_grant')

    if admin_grant and signed_by != network_id:
        # Ongoing invite with admin_grant - verify signer is admin
        # The signer's user_id should match the admin_grant's user_id
        signer_user_id = signer.get('user_id')
        grant_row = ctx.deps.get('admin_grant')
        if not grant_row or not signer_user_id:
            # Can't validate admin chain - let legacy projector handle
            pass
        elif grant_row.get('user_id') != signer_user_id:
            # Admin grant doesn't authorize this signer
            return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='invites',
            values={
                'invite_id': ctx.event_id,
                'invite_pubkey': invite_pubkey,
                'group_id': group_id or '',  # Empty string if None (schema has NOT NULL)
                'inviter_id': inviter_id,
                'mode': mode,
                'user_id': user_id,  # For mode='peer'
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


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
    # Get network_id for admin table lookup
    network_id = network.get_network_id(recorded_by, db)
    if not network_id:
        return False

    # Get user_id for this peer_shared_id
    user_id = peer_shared.get_user_id(peer_shared_id, recorded_by, db)
    if not user_id:
        return False

    # Check if user has an admin event in the admins table (per spec)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    admin_row = safedb.query_one(
        "SELECT 1 FROM admins WHERE user_id = ? AND network_id = ? AND recorded_by = ? LIMIT 1",
        (user_id, network_id, recorded_by)
    )

    return admin_row is not None


def create(peer_id: str, t_ms: int, db: Any, mode: str = 'user', user_id: str | None = None) -> tuple[str, str, dict[str, Any]]:
    """Create an invite event and generate invite link.

    Automatically queries for the inviter's main group, main channel, and peer_shared_id.
    Only admins can create invites (checked via admin event chain).

    Note: Bootstrap invites use create_bootstrap_user_invite() instead, which is
    signed by network_id and doesn't require admin check.

    Note: peer_id ownership is validated by the caller (CLI session or API layer).

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

    # Get peer identity
    identity = peer_shared.get_self(peer_id, db)
    if not identity or not identity['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set in peer_self table")

    peer_shared_id = identity['peer_shared_id']

    # Get network
    network_id = network.get_network_id(peer_id, db)
    if not network_id:
        raise ValueError(f"No network found for peer {peer_id}. Cannot create invite.")

    # Get all_users group by signature (network-signed group = all_users)
    all_users_group_id = network.get_all_users_group_id(network_id, peer_id, db)

    # Get inviter's user_id
    inviter_user_id = peer_shared.get_user_id(peer_shared_id, peer_id, db)
    if not inviter_user_id:
        raise ValueError(f"User record not found for peer_shared_id {peer_shared_id}. Cannot create invite.")

    # Authorization check depends on mode:
    # - mode='user' (network join invites): requires admin
    # - mode='peer' (device linking): any user can create for their OWN user_id
    if mode == 'user':
        if not is_admin(peer_shared_id, peer_id, db):
            raise ValueError(f"Only admins can create network join invites. Peer {peer_id} is not an admin.")
    elif mode == 'peer':
        # Security: users can only create device link invites for themselves
        if user_id != inviter_user_id:
            raise ValueError(f"Cannot create device link invite for another user. You can only link your own devices.")

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
    group_row = group.get_current_key(all_users_group_id, peer_id, db)
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
    local_prekey_id, invite_private_key = group_prekey.create(peer_id, t_ms, db)  # No offset needed

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
            t_ms=t_ms,  # No offset needed - DAG deps handle ordering
            db=db,
            user_id=user_id  # User context for device linking
        )
    else:
        # User invite: context is the all_users group
        group_prekey_shared.create(
            prekey_id=local_prekey_id,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,  # No offset needed - DAG deps handle ordering
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
    # Query by recorded_by only since peer_id may be old peer_shared_id after linking
    inviter_prekey_shared_row = safedb.query_one(
        "SELECT transit_prekey_shared_id, created_at FROM transit_prekeys_shared WHERE recorded_by = ? ORDER BY created_at DESC LIMIT 1",
        (peer_id,)
    )

    if not inviter_prekey_shared_row:
        raise ValueError(f"No prekey_shared found for inviter {peer_id}. Cannot create invite.")

    inviter_transit_prekey_shared_id = inviter_prekey_shared_row['transit_prekey_shared_id']
    inviter_transit_prekey_shared_created_at = inviter_prekey_shared_row['created_at']

    # TODO: Address info should come from an address table or network discovery
    # Currently hardcoded for local testing; production needs proper address resolution
    inviter_ip = '127.0.0.1'
    inviter_port = 6100

    # Create minimal invite event (signed by Alice, proves authorization)
    # SLIM INVITE: Only essential fields for sync. Transit prekey + address info moved to link.
    # This keeps invite event under 488-byte UDP limit.
    invite_event_data = {
        'type': 'invite',
        'mode': mode,
        'invite_pubkey': invite_pubkey_b64,  # For user proof signature
        'invite_prekey_id': invite_prekey_id,  # Crypto hint for GKS (deterministic hash)
        'group_id': all_users_group_id,  # All users group (for adding joiner)
        'inviter_user_id': inviter_user_id,  # For admin validation during projection
        'signed_by': peer_shared_id,  # Also serves as inviter_peer_shared_id (redundancy removed)
        'signer_type': 'peer_shared',  # v2: ongoing invites are signed by peer_shared
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

    # Share ALL historical keys for the all_users group, not just the current key
    # This ensures new joiners can decrypt the group event (which was encrypted with the original key)
    # and all historical content that may have been encrypted with older keys
    all_group_keys = safedb.query(
        "SELECT key_id FROM group_keys WHERE recorded_by = ?",
        (peer_id,)
    )

    keys_shared = 0
    for key_row in all_group_keys:
        historical_key_id = key_row['key_id']
        try:
            group_key_shared.create_for_invite(
                key_id=historical_key_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                invite_id=invite_id,  # Pass invite_id to extract prekey from stored invite
                t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                db=db
            )
            keys_shared += 1
        except Exception as e:
            log.warning(f"invite.create() failed to share key {historical_key_id[:20]}...: {e}")

    log.info(f"invite.create() shared {keys_shared} group key(s) for all_users group")

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

        for group_row in group_rows:
            group_id = group_row['group_id']
            key_id_for_group = group_row['key_id']

            # Skip if we already created it above (all_users)
            if key_id_for_group == key_id:
                continue

            # Create group_key_shared sealed to invite_prekey
            group_key_shared_id = group_key_shared.create_for_invite(
                key_id=key_id_for_group,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                invite_id=invite_id,
                t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                db=db
            )
            log.info(f"invite.create() created group_key_shared {group_key_shared_id[:20]}... for group {group_id[:20]}...")

    # Get inviter's peer_shared blob to include in invite link
    # This allows Bob to immediately have Alice in his peers_shared table upon joining
    # Build invite link - metadata + keys for connection, NO blobs (those sync after connection)
    import base64

    invite_link_data = {
        'invite_id': invite_id,
        'invite_prekey_id': invite_prekey_id,
        'invite_private_key': crypto.b64encode(invite_private_key),
        'inviter_peer_shared_id': peer_shared_id,
        'network_id': network_id,
        'channel_id': channel_id,
        'key_id': key_id,
        'ip': inviter_ip,
        'port': inviter_port,
        # Transit prekey for encrypting initial sync_connect to Alice
        'inviter_transit_prekey_public_key': crypto.b64encode(inviter_prekey_public_key),
        'inviter_transit_prekey_shared_id': inviter_transit_prekey_shared_id,
        'inviter_transit_prekey_id': inviter_prekey_id,
    }

    # For mode='peer', include user_id so acceptor knows which user to link to
    if mode == 'peer':
        invite_link_data['user_id'] = user_id

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


def project(invite_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project invite event into invites table.

    Supports polymorphic signed_by:
    - signed_by=network_id (bootstrap): verify with network pubkey
    - signed_by=peer_shared_id (ongoing): verify with peer_shared pubkey
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

    # Determine verification mode based on signed_by
    signed_by = event_data.get('signed_by')
    if not signed_by:
        log.warning(f"invite.project() missing signed_by field - invalid invite")
        return None
    mode = event_data.get('mode', 'user')
    # Extract signer early for INSERT
    # For ongoing invites, signed_by IS the inviter's peer_shared_id (slim invite removes redundant field)
    # For bootstrap invites with signed_by=network_id, inviter_peer_shared_id may still be present
    inviter_id = event_data.get('signed_by')
    if inviter_id == event_data.get('network_id'):
        # Bootstrap invite signed by network - use inviter_peer_shared_id if available
        inviter_id = event_data.get('inviter_peer_shared_id') or inviter_id

    log.info(f"invite.project() validating invite mode={mode} signed_by={signed_by[:20]}...")

    # Polymorphic signed_by verification
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
            # Network blob not available yet - check if network is valid via trust anchor
            # If network_id is in valid_events (from invite_accepted), trust is established out-of-band
            network_valid = safedb.query_one(
                "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
                (network_id, recorded_by)
            )
            if network_valid:
                log.info(f"invite.project() network_id={network_id[:20]}... valid via trust anchor, skipping signature verification")
            else:
                log.warning(f"invite.project() network_id={network_id[:20]}... not available yet")
                return None
        else:
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

        # Validate admin_grant chain for ongoing invite(mode=user)
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

    # NOTE: Transit prekey projection removed from invite event (slim invite)
    # Transit prekey fields are now in the invite link and projected by user.join() / invite.accept()
    # This saves ~233 bytes in the synced invite event

    # NOTE: Invite validity comes from the cascade - when its signer (network or user) is valid,
    # the invite unblocks, projects, and becomes valid naturally. No artificial marking needed.

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

    Uniform peer linking - first peer and later peers use same flow.

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
    # Determine signer_type: user_id for first peer (bootstrap), peer_shared_id for later
    # If signer_id matches user_id, it's a bootstrap case (self-signed by user)
    signer_type = 'user' if signer_id == user_id else 'peer_shared'

    event_data = {
        'type': 'invite',
        'mode': 'peer',
        'user_id': user_id,  # Which user this peer will link to
        'invite_pubkey': crypto.b64encode(invite_pubkey),
        'signed_by': signer_id,  # user_id for first peer, peer_shared_id for later
        'signer_type': signer_type,  # v2: type of signer for verification
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
    group_id: str | None,
    channel_id: str | None,
    key_id: str | None,
    peer_id: str,
    peer_shared_id: str | None,
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
        peer_shared_id: Public peer ID of inviter (None for bootstrap self-invite)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (invite_id, invite_private_key, invite_pubkey): The invite ID and keys for user signing
    """
    # Generate keypair for this user invite
    invite_private_key, invite_pubkey = crypto.generate_keypair()

    # Create bootstrap user invite event
    event_data = {
        'type': 'invite',
        'mode': 'user',
        'network_id': network_id,
        'invite_pubkey': crypto.b64encode(invite_pubkey),
        'signed_by': network_id,  # Bootstrap: signed by network key
        'signer_type': 'network',  # v2: bootstrap invites signed by network
        'created_at': t_ms
    }

    # Only include optional fields if provided (not empty string or None)
    # Bootstrap invites may not have group/channel/key yet - they're created later
    if group_id:
        event_data['group_id'] = group_id
    if channel_id:
        event_data['channel_id'] = channel_id
    if key_id:
        event_data['key_id'] = key_id
    if peer_shared_id:
        event_data['inviter_peer_shared_id'] = peer_shared_id

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

    # Extract link data (no blobs - those sync after connection)
    invite_id = link_data['invite_id']
    invite_prekey_id = link_data['invite_prekey_id']
    invite_private_key = crypto.b64decode(link_data['invite_private_key'])
    inviter_peer_shared_id = link_data['inviter_peer_shared_id']

    # Create invite_accepted event (event-sources invite secrets for reprojection)
    # This stores inviter's transit prekey in invite_accepteds table via projection
    from events.identity import invite_accepted as invite_accepted_module
    invite_accepted_id = invite_accepted_module.create(
        invite_link_data=link_data,
        peer_id=peer_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db
    )
    log.info(f"invite.accept() created invite_accepted_id={invite_accepted_id[:20]}...")

    # Build return dict
    result = {
        'mode': mode,
        'invite_id': invite_id,
        'invite_prekey_id': invite_prekey_id,
        'invite_private_key': invite_private_key,
        'inviter_peer_shared_id': inviter_peer_shared_id,
    }

    # Include network_id (required for trust anchoring in all invite types)
    if 'network_id' in link_data:
        result['network_id'] = link_data['network_id']

    # For mode='peer', include user_id from link data
    if 'user_id' in link_data:
        result['user_id'] = link_data['user_id']

    log.info(f"invite.accept() completed for mode={mode}, peer={peer_id[:20]}...")
    return result
