"""Invite event type (shareable, encrypted)."""

# Registry metadata
EVENT_TYPE = 'invite'
SHAREABLE = True  # Invites sync to enable network membership
PROJECTION_TABLE = ('invites', 'invite_id')

from typing import Any
import json
import logging
from core import crypto
from core import store
from core import wire_format
from events.identity import peer, peer_shared, network
from events.content import channel
from events.group import group
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)



def _wire_shadow_invite(
    mode: str,
    invite_pubkey_b64: str,
    invite_prekey_id: str | None,
    group_id: str | None,
    channel_id: str | None,
    key_id: str | None,
    network_id: str | None,
    inviter_peer_shared_id: str | None,
    inviter_user_id: str | None,
    target_user_id: str | None,
    admin_grant: str | None,
    address: str | None,
    port: int | None,
) -> None:
    """Validate invite fields against the fixed-size wire payload layout."""
    if mode not in ("user", "peer"):
        raise ValueError("invite mode must be 'user' or 'peer'")
    mode_value = wire_format.INVITE_MODE_USER if mode == "user" else wire_format.INVITE_MODE_PEER
    plaintext = wire_format.encode_invite_plaintext(
        mode=mode_value,
        invite_pubkey=crypto.b64decode(invite_pubkey_b64),
        invite_prekey_id=crypto.b64decode(invite_prekey_id) if invite_prekey_id else None,
        group_id=crypto.b64decode(group_id) if group_id else None,
        channel_id=crypto.b64decode(channel_id) if channel_id else None,
        key_id=crypto.b64decode(key_id) if key_id else None,
        network_id=crypto.b64decode(network_id) if network_id else None,
        inviter_peer_shared_id=crypto.b64decode(inviter_peer_shared_id) if inviter_peer_shared_id else None,
        inviter_user_id=crypto.b64decode(inviter_user_id) if inviter_user_id else None,
        target_user_id=crypto.b64decode(target_user_id) if target_user_id else None,
        admin_grant_id=crypto.b64decode(admin_grant) if admin_grant else None,
        inviter_ip=address,
        inviter_port=port,
    )
    decoded = wire_format.decode_invite_plaintext(plaintext)
    if decoded["mode"] != mode_value:
        raise ValueError("wire shadow decode mode mismatch")

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

    # Network address for bootstrap connections (ongoing invites only)
    address = event_data.get('address')
    port = event_data.get('port')

    _wire_shadow_invite(
        mode=mode,
        invite_pubkey_b64=invite_pubkey,
        invite_prekey_id=event_data.get('invite_prekey_id'),
        group_id=group_id,
        channel_id=event_data.get('channel_id'),
        key_id=event_data.get('key_id'),
        network_id=network_id,
        inviter_peer_shared_id=event_data.get('inviter_peer_shared_id'),
        inviter_user_id=event_data.get('inviter_user_id'),
        target_user_id=user_id,
        admin_grant=event_data.get('admin_grant'),
        address=address,
        port=port,
    )

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
                'address': address,  # May be None for bootstrap invites
                'port': port,  # May be None for bootstrap invites
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


def create(peer_id: str, t_ms: int, db: Any, mode: str = 'user', user_id: str | None = None,
           address: str | None = None, port: int | None = None) -> tuple[str, str, dict[str, Any]]:
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
        address: Inviter's IP address (for connection bootstrapping)
        port: Inviter's port (for connection bootstrapping)

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
    if not wire_format.is_wire_group_prekey_envelope(prekey_blob):
        raise ValueError("invite requires wire group_prekey event")
    prekey_data = wire_format.decode_group_prekey_wire_event(prekey_blob)
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
    # Query prekey from connection_prekeys table
    inviter_prekey_row = unsafedb.query_one(
        "SELECT connection_prekey_id, public_key FROM connection_prekeys WHERE owner_peer_id = ? ORDER BY created_at DESC LIMIT 1",
        (peer_id,)
    )

    if not inviter_prekey_row:
        raise ValueError(f"No prekey found for inviter {peer_id}. Cannot create invite.")

    inviter_prekey_id = inviter_prekey_row['connection_prekey_id']
    inviter_prekey_public_key = inviter_prekey_row['public_key']  # Raw bytes from DB

    # Get prekey_shared_id from connection_prekeys_shared table
    # Query by recorded_by only since peer_id may be old peer_shared_id after linking
    inviter_prekey_shared_row = safedb.query_one(
        "SELECT connection_prekey_shared_id, created_at FROM connection_prekeys_shared WHERE recorded_by = ? ORDER BY created_at DESC LIMIT 1",
        (peer_id,)
    )

    if not inviter_prekey_shared_row:
        raise ValueError(f"No prekey_shared found for inviter {peer_id}. Cannot create invite.")

    inviter_connection_prekey_shared_id = inviter_prekey_shared_row['connection_prekey_shared_id']
    inviter_connection_prekey_shared_created_at = inviter_prekey_shared_row['created_at']

    # Address info: use passed parameters or fall back to defaults
    # For production, this should come from self_address discovery
    inviter_ip = address if address is not None else '127.0.0.1'
    inviter_port = port if port is not None else 6100

    # Sign the invite event with inviter's peer private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    _wire_shadow_invite(
        mode=mode,
        invite_pubkey_b64=invite_pubkey_b64,
        invite_prekey_id=invite_prekey_id,
        group_id=all_users_group_id,
        channel_id=None,
        key_id=None,
        network_id=None,
        inviter_peer_shared_id=None,
        inviter_user_id=inviter_user_id,
        target_user_id=user_id,
        admin_grant=admin_grant_id,
        address=inviter_ip,
        port=inviter_port,
    )

    invite_blob = wire_format.encode_invite_wire_event(
        mode=mode,
        invite_pubkey_b64=invite_pubkey_b64,
        invite_prekey_id_b64=invite_prekey_id,
        group_id_b64=all_users_group_id,
        channel_id_b64=None,
        key_id_b64=None,
        network_id_b64=None,
        inviter_peer_shared_id_b64=None,
        inviter_user_id_b64=inviter_user_id,
        target_user_id_b64=user_id,
        admin_grant_id_b64=admin_grant_id,
        inviter_ip=inviter_ip,
        inviter_port=inviter_port,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )
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

    # Build invite link - metadata + keys for connection
    import base64

    invite_link_data = {
        'invite_id': invite_id,
        'invite_blob': base64.urlsafe_b64encode(invite_blob).decode().rstrip('='),
        'invite_prekey_id': invite_prekey_id,
        'invite_private_key': crypto.b64encode(invite_private_key),
        'inviter_peer_shared_id': peer_shared_id,
        'network_id': network_id,
        'channel_id': channel_id,
        'key_id': key_id,
        'ip': inviter_ip,
        'port': inviter_port,
        # Transit prekey for encrypting initial sync_connect to Alice
        'inviter_connection_prekey_public_key': crypto.b64encode(inviter_prekey_public_key),
        'inviter_connection_prekey_shared_id': inviter_connection_prekey_shared_id,
        'inviter_connection_prekey_id': inviter_prekey_id,
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

    _wire_shadow_invite(
        mode="peer",
        invite_pubkey_b64=crypto.b64encode(invite_pubkey),
        invite_prekey_id=None,
        group_id=None,
        channel_id=None,
        key_id=None,
        network_id=None,
        inviter_peer_shared_id=None,
        inviter_user_id=None,
        target_user_id=user_id,
        admin_grant=None,
        address=None,
        port=None,
    )

    blob = wire_format.encode_invite_wire_event(
        mode="peer",
        invite_pubkey_b64=crypto.b64encode(invite_pubkey),
        invite_prekey_id_b64=None,
        group_id_b64=None,
        channel_id_b64=None,
        key_id_b64=None,
        network_id_b64=None,
        inviter_peer_shared_id_b64=None,
        inviter_user_id_b64=None,
        target_user_id_b64=user_id,
        admin_grant_id_b64=None,
        inviter_ip=None,
        inviter_port=None,
        signed_by_b64=signer_id,
        signer_type=signer_type,
        created_at_ms=t_ms,
        private_key=signer_private_key,
    )
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

    _wire_shadow_invite(
        mode="user",
        invite_pubkey_b64=crypto.b64encode(invite_pubkey),
        invite_prekey_id=None,
        group_id=group_id,
        channel_id=channel_id,
        key_id=key_id,
        network_id=network_id,
        inviter_peer_shared_id=peer_shared_id,
        inviter_user_id=None,
        target_user_id=None,
        admin_grant=None,
        address=None,
        port=None,
    )

    blob = wire_format.encode_invite_wire_event(
        mode="user",
        invite_pubkey_b64=crypto.b64encode(invite_pubkey),
        invite_prekey_id_b64=None,
        group_id_b64=group_id,
        channel_id_b64=channel_id,
        key_id_b64=key_id,
        network_id_b64=network_id,
        inviter_peer_shared_id_b64=peer_shared_id,
        inviter_user_id_b64=None,
        target_user_id_b64=None,
        admin_grant_id_b64=None,
        inviter_ip=None,
        inviter_port=None,
        signed_by_b64=network_id,
        signer_type="network",
        created_at_ms=t_ms,
        private_key=network_private_key,
    )
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

    # Store invite blob if present (removes dependency on sync for invite verification).
    invite_blob_b64 = link_data.get('invite_blob')
    if invite_blob_b64:
        blob_padding = '=' * (4 - len(invite_blob_b64) % 4)
        try:
            invite_blob = base64.urlsafe_b64decode(invite_blob_b64 + blob_padding)
            stored_invite_id = store.event(invite_blob, peer_id, t_ms, db)
            if stored_invite_id != link_data.get('invite_id'):
                log.warning(
                    f"invite.accept() invite_id mismatch: link={link_data.get('invite_id')[:20]}... "
                    f"stored={stored_invite_id[:20]}..."
                )
        except Exception as e:
            log.warning(f"invite.accept() failed to store invite_blob: {e}")

    # Extract link data
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
