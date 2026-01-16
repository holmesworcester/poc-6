"""User event type (shareable, encrypted) - represents network membership."""
from typing import Any

# Registry metadata
EVENT_TYPE = 'user'
SHAREABLE = True  # User events sync across the network
EPHEMERAL = False
PROJECTION_TABLE = ('users', 'user_id')
import base64
import logging
from core import crypto
from core import store
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# v2 event specification - signed by invite_id
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'invite': {
            'source': 'table',
            'table': 'invites',
            'key': 'invite_id',
            'key_from': 'invite_id',
            'fields': ['invite_id', 'invite_pubkey', 'group_id'],
        },
    },
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for user events.

    User events represent network membership, signed by invite_id.
    The resolver handles signature verification with invite_pubkey.

    Writes to: users, group_members (if invite has group_id)
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'user':
        return ProjectorResult(writes=tuple(), valid_event=False)

    invite_id = event_data.get('invite_id')
    signed_by = event_data.get('signed_by')

    if not invite_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    if signed_by != invite_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Extract user fields
    user_pubkey = event_data.get('user_pubkey', '')
    network_id = event_data.get('network_id')
    created_at = event_data.get('created_at')

    # Try to get network_id from invite dep if not in event
    invite_row = ctx.deps.get('invite')
    if not network_id and invite_row:
        # network_id might be in invite event, but invites table doesn't store it
        # Leave as None - legacy projector has more complex logic
        pass

    writes = [
        WriteOp(
            op='insert',
            table='users',
            values={
                'user_id': ctx.event_id,
                'name': '',  # Placeholder - real name from encrypted username_update
                'network_id': network_id,
                'created_at': created_at,
                'user_pubkey': user_pubkey,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    ]

    # Add to group_members if invite has group_id
    if invite_row and invite_row.get('group_id'):
        group_id = invite_row['group_id']
        inviter_peer_shared_id = invite_row.get('inviter_id', signed_by)
        writes.append(
            WriteOp(
                op='insert',
                table='group_members',
                values={
                    'member_id': ctx.event_id,  # Use user_id as member_id
                    'group_id': group_id,
                    'user_id': ctx.event_id,
                    'added_by': inviter_peer_shared_id,
                    'created_at': created_at,
                    'recorded_by': ctx.recorded_by,
                    'recorded_at': ctx.recorded_at,
                },
            )
        )

    return ProjectorResult(writes=tuple(writes), valid_event=True)


def create(peer_id: str, name: str, t_ms: int, db: Any,
           invite_id: str,
           invite_private_key: bytes,
           network_id: str | None = None,
           # Deprecated parameters kept for compatibility - not used
           peer_shared_id: str | None = None) -> tuple[str, bytes]:
    """Create a user event representing network membership.

    User events are signed by invite (signed_by=invite_id) with user's own keypair.
    - invite_private_key: proves possession of invite link
    - user_pubkey: user's own unique public key (stored in event body)
    - user_private_key: returned to caller for signing first peer invite

    This function is isomorphic for both:
    1. Network creator (Alice): Via bootstrap self-invite
    2. Invite joiner (Bob): Via invite link from existing member

    Args:
        peer_id: Local peer ID (for recording)
        name: Display name for the user
        t_ms: Timestamp
        db: Database connection
        invite_id: Reference to invite event (required - all users join via invite)
        invite_private_key: Invite private key for signing (required - proves invite possession)
        network_id: Network ID from invite link (passed directly to avoid reading invite blob)
        peer_shared_id: Deprecated, not used (kept for compatibility)

    Returns:
        (user_id, user_private_key): The stored user event ID and user_private_key
        for caller to sign first peer invite
    """
    # All users must join via invite
    if not invite_id:
        raise ValueError("invite_id is required - all users must join via invite")
    if not invite_private_key:
        raise ValueError("invite_private_key is required - proves possession of invite")

    # network_id is now passed as parameter from invite link data
    # This avoids reading the invite blob which may not be available in distributed scenarios

    # Generate user's OWN unique keypair
    # This is separate from invite keypair - each user has their own identity
    user_private_key, user_pubkey = crypto.generate_keypair()

    # Create user event with signed_by=invite_id and user_pubkey
    # NOTE: user event does NOT contain peer_id - the user→peer relationship
    # is established when peer_shared is projected (user_id stored in peers_shared table)
    # NOTE: name is NOT included in event - usernames are transmitted via encrypted
    # username_update events to protect privacy from NETWORK ACTIVE ATTACKER
    event_data = {
        'type': 'user',
        'invite_id': invite_id,  # Reference to invite that authorized this user
        'signed_by': invite_id,  # Polymorphic signer field (verified with invite_pubkey)
        'signer_type': 'invite',  # v2: user events are signed by invite
        'user_pubkey': crypto.b64encode(user_pubkey),  # User's OWN public key (for signing first peer invite)
        'created_at': t_ms
    }

    # Add network_id if present
    if network_id:
        event_data['network_id'] = network_id

    # Sign with invite_private_key (proves possession of invite link)
    # NOT with peer's private key - the signature proves invite possession
    signed_event = crypto.sign_event(event_data, invite_private_key)

    # Store as signed plaintext (no inner encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    user_id = store.event(blob, peer_id, t_ms, db)

    # Note: peer_self.user_id is now set by peer_shared.project() when the peer_shared
    # is signed by an invite(mode=peer). This makes the first device use the same flow
    # as subsequent devices (uniform peer linking).

    # Transit keys are now created by peer_shared.join() (canonical operation)
    # This avoids duplication and makes peer_shared.join() the complete operation

    # Return user_private_key for caller to sign first peer invite
    return user_id, user_private_key


def project(user_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project user event into users table.

    User events are verified using invite_pubkey (not peer_shared pubkey).
    The user_pubkey from the event body is stored for signing first peer invite.
    """
    log.warning(f"[USER_PROJECT_ENTRY] user.project() called: user_id={user_id[:20]}..., recorded_by={recorded_by[:20]}...")

    # Get blob from store
    blob = store.get(user_id, db)
    if not blob:
        log.warning(f"[USER_PROJECT_EARLY_RETURN] Blob not found for user_id={user_id[:20]}...")
        return None

    # Parse JSON (signed plaintext, no decryption needed)
    event_data = crypto.parse_json(blob)

    # All user events must have invite_id and signed_by=invite_id
    invite_id = event_data.get('invite_id')
    signed_by = event_data.get('signed_by')

    if not invite_id:
        log.warning(f"[USER_PROJECT_EARLY_RETURN] Missing invite_id - invalid user event")
        return None

    if signed_by != invite_id:
        log.warning(f"[USER_PROJECT_EARLY_RETURN] signed_by={signed_by} doesn't match invite_id={invite_id}")
        return None

    # Get invite_pubkey to verify signature
    # First try invites table (projected invite), then fall back to store blob (bootstrap)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    invite_pubkey_bytes = None

    invite_row = safedb.query_one(
        "SELECT invite_pubkey FROM invites WHERE invite_id = ? AND recorded_by = ? LIMIT 1",
        (invite_id, recorded_by)
    )

    if invite_row:
        invite_pubkey_bytes = crypto.b64decode(invite_row['invite_pubkey'])
    else:
        # Invite not in table yet - try to get pubkey from invite blob in store
        # This handles bootstrap case where invite_accepted hasn't unblocked invite projection yet
        invite_blob = store.get(invite_id, db)
        if invite_blob:
            invite_data = crypto.parse_json(invite_blob)
            invite_pubkey_b64 = invite_data.get('invite_pubkey')
            if invite_pubkey_b64:
                invite_pubkey_bytes = crypto.b64decode(invite_pubkey_b64)
                log.info(f"[USER_PROJECT] Got invite_pubkey from store blob (bootstrap case)")

    # Fallback: Derive invite_pubkey from invite_private_key in invite_accepteds
    # This handles distributed scenario where invite blob hasn't synced yet
    # but we accepted the invite ourselves (have the private key)
    if not invite_pubkey_bytes:
        ia_row = safedb.query_one(
            "SELECT invite_private_key FROM invite_accepteds WHERE invite_id = ? AND recorded_by = ?",
            (invite_id, recorded_by)
        )
        if ia_row and ia_row['invite_private_key']:
            from nacl.signing import SigningKey
            priv_key = ia_row['invite_private_key']
            signing_key = SigningKey(priv_key)
            invite_pubkey_bytes = bytes(signing_key.verify_key)
            log.info(f"[USER_PROJECT] Derived invite_pubkey from invite_accepteds (distributed bootstrap)")

    if not invite_pubkey_bytes:
        # Neither table, store, nor invite_accepteds has invite - return None, will retry later
        log.warning(f"[USER_PROJECT_EARLY_RETURN] invite_id={invite_id[:20]}... not available yet")
        return None

    # Verify signature using invite_pubkey (proves possession of invite link)
    if not crypto.verify_event(event_data, invite_pubkey_bytes):
        log.warning(f"[USER_PROJECT_EARLY_RETURN] Signature verification failed using invite_pubkey")
        return None

    log.info(f"[USER_PROJECT] Signature verified with invite_pubkey from invites table")

    # Extract user_pubkey from event body (user's OWN unique public key)
    user_pubkey = event_data.get('user_pubkey', '')

    # Get network_id from event or from invite
    network_id = event_data.get('network_id')
    if not network_id:
        # Try to get from invite event blob
        invite_blob = store.get(invite_id, db)
        if invite_blob:
            invite_data = crypto.parse_json(invite_blob)
            network_id = invite_data.get('network_id')

    # Insert into users table (user→peer relationship is stored in peers_shared table, populated by peer_shared.project())
    # Note: name is empty - actual usernames come from encrypted username_update events (stored in user_names table)
    log.warning(f"[USER_PROJECT_INSERT] Inserting user into users table: user_id={user_id[:20]}...")
    safedb.execute(
        """INSERT OR IGNORE INTO users
           (user_id, name, network_id, created_at, user_pubkey, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            '',  # Placeholder - real name from encrypted username_update
            network_id,
            event_data['created_at'],
            user_pubkey,  # User's OWN public key (for verifying signed_by=user_id)
            recorded_by,
            recorded_at
        )
    )
    log.warning(f"[USER_PROJECT_SUCCESS] User inserted successfully")

    # Note: Admin grants for first_peer are handled explicitly in new_network()
    # after the user is created, not as a side effect here.

    # Add to group_members (all_users group from invite)
    # invite_proof was removed, so we add group membership here for all users
    # Get group_id from invite event
    invite_blob = store.get(invite_id, db)
    if invite_blob:
        invite_data = crypto.parse_json(invite_blob)
        group_id = invite_data.get('group_id')
        if group_id:
            # Get inviter_peer_shared_id for added_by field
            inviter_peer_shared_id = invite_data.get('inviter_peer_shared_id', event_data.get('signed_by', ''))
            safedb.execute(
                """INSERT OR IGNORE INTO group_members
                   (member_id, group_id, user_id, added_by, created_at, recorded_by, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,  # Use user_id as member_id
                    group_id,
                    user_id,
                    inviter_peer_shared_id,  # Added by inviter
                    event_data['created_at'],
                    recorded_by,
                    recorded_at
                )
            )
            log.info(f"user.project() added user {user_id[:20]}... to group {group_id[:20]}...")

    # NOTE: validity is handled by recorded.project() after successful projection
    return user_id

def new_network(name: str, t_ms: int, db: Any, device_name: str = "Device", network_name: str | None = None) -> dict[str, Any]:
    """Create a new user with their own implicit network.

    Simplified bootstrap: minimal identity events first, content after peer_shared exists.

    Bootstrap (identity only, before peer_shared exists):
    1. peer.create() -> peer_id only (NO peer_shared)
    2. network.create() -> self-signed network (root of trust)
    3. invite (bootstrap user) -> signed by network
    4. join() -> creates user_id
    5. invite (mode=peer) -> signed by user
    6. peer_shared -> signed by invite (THE canonical peer_shared)

    Content setup (after peer_shared exists):
    7. admin_grant -> signed by network (grants admin to first user)
    8. group (all_users) -> signed by peer_shared_id
    9. channel -> signed by peer_shared_id
    10. transit_prekey + transit_prekey_shared -> for sync

    Args:
        name: Username/display name
        t_ms: Base timestamp (each event gets incremented)
        db: Database connection
        device_name: Device name (e.g., "Phone", "Desktop")
        network_name: Network display name (optional)

    Returns:
        {
            'peer_id': str,
            'peer_shared_id': str,
            'prekey_id': str,
            'network_id': str,
            'all_users_group_id': str,
            'channel_id': str,
            'user_id': str,
        }
    """
    from events.group import group
    from events.identity import network, invite, admin
    from events.content import channel

    log.info(f"new_network() creating network for '{name}' at t_ms={t_ms} (simplified bootstrap)")

    # =========================================================================
    # Bootstrap (identity only)
    # =========================================================================

    # 1. Create peer (local only - NO peer_shared)
    peer_id = peer.create(t_ms=t_ms, db=db)
    log.info(f"new_network() created peer: {peer_id[:20]}...")

    # 2. Create NETWORK event (self-signed root of trust)
    # Network is minimal - just identity with its own keypair
    network_id, network_private_key = network.create(
        peer_id=peer_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db
    )
    log.info(f"new_network() created self-signed network: {network_id[:20]}...")

    # Note: Network event blocks naturally on dependencies (will be projected when valid).
    # No manual insertion needed - trust the projection system to handle it.
    # This eliminates the "fudging" and lets dependency blocking work as designed.

    # Create safedb for bootstrap operations (join needs it)
    from core.db import create_safe_db
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Stage 3b: Create bootstrap user invite WITHOUT placeholder IDs
    # Don't reference content events that don't exist yet - joiners discover them via sync
    # This eliminates the placeholder IDs (empty strings) entirely
    invite_id, invite_private_key, invite_pubkey = invite.create_bootstrap_user_invite(
        network_id=network_id,
        network_private_key=network_private_key,
        group_id=None,  # Not available yet - content created after peer_shared
        channel_id=None,  # Not available yet - content created after peer_shared
        key_id=None,  # Not available yet - content created after peer_shared
        peer_id=peer_id,
        peer_shared_id=None,  # Bootstrap: no inviter (self-invite)
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db
    )
    log.info(f"new_network() created bootstrap user invite: {invite_id[:20]}...")

    # Build invite_link for join() - must match the format expected by join()
    import json
    invite_blob = store.get(invite_id, db)

    # Generate deterministic prekey ID from public key hash (for bootstrap user invite)
    # This is needed for invite_accepted.project() to restore invite secrets
    bootstrap_invite_prekey_id = crypto.b64encode(crypto.hash(invite_pubkey)[:16])

    invite_link_data = {
        'invite_blob': base64.urlsafe_b64encode(invite_blob).decode().rstrip('='),
        'invite_id': invite_id,
        'invite_prekey_id': bootstrap_invite_prekey_id,
        'invite_private_key': crypto.b64encode(invite_private_key),
        'inviter_peer_shared_id': None,  # Bootstrap: no inviter (self-invite)
        'ip': '127.0.0.1',
        'port': 6100,
    }
    invite_json = json.dumps(invite_link_data, separators=(',', ':'), sort_keys=True)
    invite_code = base64.urlsafe_b64encode(invite_json.encode()).decode().rstrip('=')
    invite_link = f"quiet://invite/{invite_code}"
    log.info(f"new_network() built invite_link with invite_prekey_id={bootstrap_invite_prekey_id[:20]}...")

    # 4. Create user via bootstrap invite (isomorphic with join() flow)
    # Note: peer_self is populated later by peer_shared.project() - no PENDING entry needed
    user_id, user_private_key = create(
        peer_id=peer_id,
        name=name,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db,
        invite_id=invite_id,
        invite_private_key=invite_private_key,
        network_id=network_id  # Pass directly (available locally in new_network)
    )
    log.info(f"new_network() created user: user_id={user_id[:20]}...")

    # 5-7. Delegate to peer_shared.join() for canonical peer creation flow
    # This reuses the same code as user.join(), eliminating duplication
    # Create peer invite (mode=peer) signed by user_id
    peer_invite_id, peer_invite_private_key, _ = invite.create_peer_invite(
        user_id=user_id,
        signer_id=user_id,  # First peer: signed by user
        signer_private_key=user_private_key,
        peer_id=peer_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db
    )
    log.info(f"new_network() created peer invite: {peer_invite_id[:20]}... signed by user_id")

    # NOTE: invite projection happens via store.event() -> recorded.project() cascade
    # No direct invite.project() call needed

    # Delegate to peer_shared.join() - the canonical peer-joining operation
    # (reused by both user.join() and user.new_network())
    from events.identity import peer_shared
    peer_shared_join_result = peer_shared.join(
        peer_id=peer_id,
        peer_invite_id=peer_invite_id,
        peer_invite_private_key=peer_invite_private_key,
        user_id=user_id,
        prekey_id=bootstrap_invite_prekey_id,  # Bootstrap user invite's prekey ID
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db,
        device_name=device_name
    )
    peer_shared_id = peer_shared_join_result['peer_shared_id']
    log.info(f"new_network() delegated to peer_shared.join(): {peer_shared_id[:20]}...")

    # =========================================================================
    # Content setup (after peer_shared exists)
    # =========================================================================

    from events.network import recorded

    # 7. Create admin_grant event signed by network (grants admin to first user)
    admin_grant_id = admin.create(
        user_id=user_id,
        network_id=network_id,
        signed_by=network_id,
        signer_private_key=network_private_key,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        peer_id=peer_id,
        db=db
    )
    log.info(f"new_network() created admin_grant: {admin_grant_id[:20]}...")

    # NOTE: admin.create() calls store.event() which already projects via recorded.project()
    # No explicit projection needed - deps (user_id, signed_by=network_id) are valid

    # 8. Create ALL_USERS group (main group for all users)
    # NETWORK-SIGNED: This group is signed by network_id (using network_private_key)
    # This is how joiners discover the all_users group: query WHERE signed_by = network_id
    all_users_group_id, all_users_key_id = group.create(
        name=f"{name}",
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db,
        is_main=True,
        network_id=network_id,
        signer_id=network_id,  # Sign with network key (cryptographic role marker)
        signer_private_key=network_private_key
    )
    log.info(f"new_network() created network-signed all_users group: {all_users_group_id[:20]}...")

    # Create username_update for the network creator now that the group key is available
    # This ensures the creator has a username before they can send messages
    from events.identity import username_update
    try:
        username_update_id = username_update.create(
            user_id=user_id,
            name=name,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,  # No offset needed - DAG deps handle ordering
            db=db
        )
        # NOTE: username_update.create() calls store.event() which already projects
        log.info(f"new_network() created username_update: {username_update_id[:20]}...")
    except username_update.KeyNotAvailableError:
        # This shouldn't happen during bootstrap since we just created the key
        log.warning(f"new_network() key not available for username_update - unexpected during bootstrap")

    # Create network_name_update if network_name provided
    if network_name:
        from events.identity import network_name_update
        try:
            network_name_update_id = network_name_update.create(
                network_id=network_id,
                name=network_name,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                db=db
            )
            # NOTE: network_name_update.create() calls store.event() which already projects
            log.info(f"new_network() created network_name_update: {network_name_update_id[:20]}...")
        except network_name_update.KeyNotAvailableError:
            log.warning(f"new_network() key not available for network_name_update - unexpected during bootstrap")

    # NOTE: peer_name_update for device_name is created by peer_shared.join() above

    # 9. Create default channel (normal path - no bootstrap special case)
    # Pass admin_grant directly so the event has explicit dependency for convergence
    channel_id = channel.create(
        name='general',
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db,
        is_main=True,
        admin_grant=admin_grant_id  # Explicit dependency for convergence
    )
    log.info(f"new_network() created channel: {channel_id[:20]}...")

    # 11. Create transit_prekey + transit_prekey_shared (for sync)
    from events.network import transit_prekey, transit_prekey_shared
    prekey_id, prekey_private = transit_prekey.create(
        peer_id=peer_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db
    )
    transit_prekey_shared_id = transit_prekey_shared.create(
        prekey_id=prekey_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db
    )
    log.info(f"new_network() created transit prekey: {prekey_id[:20]}..., shared={transit_prekey_shared_id[:20]}...")

    # Add user to all_users group
    # Pass admin_grant directly so the event has explicit dependency for convergence
    from events.group import group_member
    all_users_member_id = group_member.create(
        group_id=all_users_group_id,
        user_id=user_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db,
        # NOTE: skip_admin_check removed - admin_grant is already projected by this point
        admin_grant=admin_grant_id  # Explicit dependency for convergence
    )
    log.info(f"new_network() added user to all_users group: {all_users_member_id[:20]}...")

    db.commit()

    return {
        'peer_id': peer_id,
        'peer_shared_id': peer_shared_id,
        'prekey_id': prekey_id,
        'transit_prekey_shared_id': transit_prekey_shared_id,
        'network_id': network_id,
        'all_users_group_id': all_users_group_id,
        'channel_id': channel_id,
        'user_id': user_id,
        'invite_id': invite_id,
        'admin_grant_id': admin_grant_id,
        # Convenience aliases for all_users group
        'group_id': all_users_group_id,
        'key_id': all_users_key_id,
    }


def try_create_username(user_id: str, name: str, peer_id: str, peer_shared_id: str, t_ms: int,
                        db: Any) -> tuple[str | None, bool]:
    """Try to create a username_update event, or store pending if key unavailable.

    Args:
        user_id: The user to set the name for
        name: The username
        peer_id: Local peer ID
        peer_shared_id: Public peer ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        (username_update_id or None, was_stored_pending: bool)
    """
    from events.identity import username_update
    from core.db import create_safe_db

    safedb = create_safe_db(db, recorded_by=peer_id)

    try:
        username_update_id = username_update.create(
            user_id=user_id,
            name=name,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,
            db=db
        )
        log.info(f"try_create_username() created username_update: {username_update_id[:20]}...")
        return username_update_id, False

    except username_update.KeyNotAvailableError:
        # Key not available yet - store for later creation
        log.info(f"try_create_username() key not available, storing in pending_name_updates")
        import hashlib
        pending_id = hashlib.sha256(f"{user_id}:username:{t_ms}".encode()).hexdigest()[:20]
        safedb.execute(
            """INSERT OR IGNORE INTO pending_name_updates
               (id, type, entity_id, name, peer_id, peer_shared_id, status, created_at, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pending_id, 'username', user_id, name, peer_id, peer_shared_id,
             'waiting_for_key', t_ms, peer_id, t_ms)
        )
        return None, True


def get_display_name(user_id: str, recorded_by: str, db: Any) -> str | None:
    """Return the best available display name for a user_id.

    Prefers decrypted usernames (user_names). Falls back to users.name if present.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT name FROM user_names WHERE user_id = ? AND recorded_by = ? LIMIT 1",
        (user_id, recorded_by),
    )
    if row and row.get("name"):
        return row["name"]

    row = safedb.query_one(
        "SELECT name FROM users WHERE user_id = ? AND recorded_by = ? LIMIT 1",
        (user_id, recorded_by),
    )
    if row and row.get("name"):
        return row["name"]

    return None


def join(peer_id: str, invite_link: str, name: str, t_ms: int, db: Any,
         device_name: str = "Device") -> dict[str, Any]:
    """Join an existing network via invite link.

    Peer must be created by caller before calling join().
    This ensures consistent flow for both network creators and joiners.

    Creates:
    - user (membership with invite proof, auto-creates prekey + prekey_shared)

    The inviter will share:
    - key (via key_shared event)
    - group, channel events (via sync)

    Args:
        peer_id: Local peer ID (must already exist - create with peer.create() first)
        invite_link: Invite link from network creator (format: "quiet://invite/{base64-json}")
        name: Username/display name
        t_ms: Base timestamp
        db: Database connection
        device_name: Device name (e.g., "Phone", "Desktop")

    Returns:
        {
            'peer_id': str,
            'peer_shared_id': str,
            'user_id': str,
            'invite_data': dict,
        }
    """
    log.info(f"join() user '{name}' joining via invite at t_ms={t_ms} with peer_id={peer_id[:20]}...")

    from events.identity import invite

    # Verify peer was created (check local_peers table)
    from core.db import create_safe_db
    safedb = create_safe_db(db, recorded_by=peer_id)
    unsafedb = create_unsafe_db(db)
    peer_exists = unsafedb.query_one("SELECT 1 FROM local_peers WHERE peer_id = ?", (peer_id,))
    if not peer_exists:
        raise ValueError(f"Peer {peer_id} not found. Create peer with peer.create() before calling join().")
    # Note: peer_self is populated later by peer_shared.project() - no PENDING entry needed

    # Parse invite link
    import base64
    import json

    if not invite_link.startswith('quiet://invite/'):
        raise ValueError(f"Invalid invite link format: {invite_link}")

    invite_code = invite_link.replace('quiet://invite/', '')
    # Add back padding if needed
    padding = (4 - len(invite_code) % 4) % 4
    invite_code_padded = invite_code + ('=' * padding)

    try:
        invite_json = base64.urlsafe_b64decode(invite_code_padded).decode()
        invite_data = json.loads(invite_json)
    except Exception as e:
        raise ValueError(f"Failed to decode invite link: {e}")

    # Extract metadata from link (no blobs - those sync after connection)
    invite_id = invite_data['invite_id']
    invite_prekey_id = invite_data['invite_prekey_id']
    invite_private_key = crypto.b64decode(invite_data['invite_private_key'])
    inviter_peer_shared_id = invite_data['inviter_peer_shared_id']
    channel_id = invite_data.get('channel_id')
    key_id = invite_data.get('key_id')

    log.info(f"join() extracted invite_id={invite_id[:20]}... from invite link")

    # Create group_prekey event from invite key material so we can decrypt group_key_shared events
    # The invite_prekey_id was used by Alice when wrapping group_key_shared to us
    # Since group_prekey blobs are deterministic (same key material = same hash),
    # this produces the SAME prekey_id that Alice created
    from nacl.signing import SigningKey
    from events.group import group_prekey
    signing_key = SigningKey(invite_private_key)
    invite_pubkey = bytes(signing_key.verify_key)
    created_prekey_id = group_prekey.create_from_material(
        public_key=invite_pubkey,
        private_key=invite_private_key,
        peer_id=peer_id,
        t_ms=t_ms,
        db=db
    )
    log.info(f"join() created group_prekey from invite material: {created_prekey_id[:20]}... (should match invite_prekey_id={invite_prekey_id[:20]}...)")

    # Create invite_accepted event to capture invite link data for event-sourcing
    # This stores inviter's transit prekey in invite_accepteds table via projection
    # invite_private_key is stored in invite_accepteds table via projection
    # sync_connect.send() queries invite_accepteds by invite_id to get the signing key
    from events.identity import invite_accepted
    invite_accepted_id = invite_accepted.create(
        invite_link_data=invite_data,  # Complete raw invite link data
        peer_id=peer_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db
    )

    # 2. Create user membership (user event is signed by invite)
    # Returns user_private_key for signing first peer invite
    user_id, user_private_key = create(
        peer_id=peer_id,
        name=name,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db,
        invite_id=invite_id,
        invite_private_key=invite_private_key,
        network_id=invite_data.get('network_id')  # Pass from invite link data
    )

    # invite_proof removed - proof IS the signature on user event (signed_by=invite_id)

    log.info(f"join() user '{name}' joined: peer={peer_id[:20]}...")

    # =========================================================================
    # Complete isomorphic bootstrap - delegate to peer_shared.join()
    # =========================================================================

    # 5. Create invite (mode=peer) signed by user_id
    # For first peer, signer_id=user_id (user signs invite for their own peer)
    peer_invite_id, peer_invite_private_key, _ = invite.create_peer_invite(
        user_id=user_id,
        signer_id=user_id,  # For first peer, user signs the peer invite
        signer_private_key=user_private_key,
        peer_id=peer_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db
    )
    log.info(f"join() created peer invite: {peer_invite_id[:20]}... signed by user_id")

    # NOTE: invite projection happens via store.event() -> recorded.project() cascade
    # No direct invite.project() call needed

    # 6. Delegate to peer_shared.join() for peer_shared creation and transit keys
    # This is the canonical operation reused for both first peer and device linking
    from events.identity import peer_shared
    peer_shared_join_result = peer_shared.join(
        peer_id=peer_id,
        peer_invite_id=peer_invite_id,
        peer_invite_private_key=peer_invite_private_key,
        user_id=user_id,
        prekey_id=invite_prekey_id,  # From user invite (for dependency tracking)
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db,
        device_name=device_name
    )
    peer_shared_id = peer_shared_join_result['peer_shared_id']
    prekey_id = peer_shared_join_result['transit_prekey_id']
    transit_prekey_shared_id = peer_shared_join_result['transit_prekey_shared_id']
    log.info(f"join() delegated to peer_shared.join(): peer_shared_id={peer_shared_id[:20]}...")

    # Try to create username_update event (encrypted with group key)
    # If key is not available yet, store in pending_name_updates table for later
    from events.identity import username_update
    try:
        username_update_id = username_update.create(
            user_id=user_id,
            name=name,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,  # No offset needed - DAG deps handle ordering
            db=db
        )
        log.info(f"join() created username_update: {username_update_id[:20]}...")
    except username_update.KeyNotAvailableError:
        # Key not available yet - store for later creation when group_key_shared arrives
        log.info(f"join() key not available yet, storing username intent in pending_name_updates")
        import hashlib
        pending_id = hashlib.sha256(f"{user_id}:username:{t_ms}".encode()).hexdigest()[:20]
        safedb.execute(
            """INSERT OR IGNORE INTO pending_name_updates
               (id, type, entity_id, name, peer_id, peer_shared_id, status, created_at, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pending_id, 'username', user_id, name, peer_id, peer_shared_id,
             'waiting_for_key', t_ms, peer_id, t_ms)
        )
        log.info(f"join() stored pending username for user {user_id[:20]}...")

    # NOTE: peer_name_update for device_name is created by peer_shared.join() above

    return {
        'peer_id': peer_id,
        'peer_shared_id': peer_shared_id,
        'user_id': user_id,
        'prekey_id': prekey_id,
        'transit_prekey_shared_id': transit_prekey_shared_id,
        'network_id': invite_data.get('network_id'),  # From invite
        'channel_id': channel_id,
        'key_id': key_id,
        'invite_data': invite_data,
        'invite_accepted_id': invite_accepted_id,
    }
