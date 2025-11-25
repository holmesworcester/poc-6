"""User event type (shareable, encrypted) - represents network membership."""
from typing import Any
import base64
import logging
import crypto
import store
from events.network import transit_key
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, peer_shared_id: str, name: str, t_ms: int, db: Any,
           invite_id: str | None = None,
           invite_private_key: bytes | None = None,
           group_id: str | None = None, channel_id: str | None = None,
           network_id: str | None = None) -> tuple[str, str, str, bytes]:
    """Create a user event representing network membership.

    Also auto-creates a prekey for receiving sync requests.

    Phase 2 design: User events are signed by invite (signed_by=invite_id) with user's own keypair.
    - invite_private_key: proves possession of invite link
    - user_pubkey: user's own unique public key (stored in event body)
    - user_private_key: returned to caller for signing first peer invite (Phase 3)

    Two modes:
    1. Invite joiner (Bob): Requires invite_id, invite_private_key. Metadata from invite.
    2. Network creator (Alice): Uses same flow via self-invite (Phase 6).

    Args:
        peer_id: Local peer ID (for recording)
        peer_shared_id: Public peer ID (for created_by and peer_id in event)
        name: Display name for the user
        t_ms: Timestamp
        db: Database connection
        invite_id: Reference to invite event (required - all users join via invite)
        invite_private_key: Invite private key for signing (required - proves invite possession)
        group_id: Group ID (legacy, extracted from invite)
        channel_id: Channel ID (legacy, extracted from invite)
        network_id: Network ID (legacy, extracted from invite)

    Returns:
        (user_id, transit_prekey_shared_id, transit_prekey_id, user_private_key):
        The stored user event ID, transit_prekey_shared ID, transit_prekey ID,
        and user_private_key (for caller to sign first peer invite)
    """
    # Phase 2: All users must join via invite
    if not invite_id:
        raise ValueError("invite_id is required - all users must join via invite")
    if not invite_private_key:
        raise ValueError("invite_private_key is required - proves possession of invite")

    # Extract metadata from invite
    invite_blob = store.get(invite_id, db)
    if not invite_blob:
        raise ValueError(f"invite event not found: {invite_id}")

    invite_event_data = crypto.parse_json(invite_blob)
    group_id = invite_event_data['group_id']
    channel_id = invite_event_data['channel_id']
    key_id = invite_event_data['key_id']
    network_id = invite_event_data.get('network_id')

    # Phase 2: Generate user's OWN unique keypair
    # This is separate from invite keypair - each user has their own identity
    user_private_key, user_pubkey = crypto.generate_keypair()

    # Create user event with signed_by=invite_id and user_pubkey
    event_data = {
        'type': 'user',
        'invite_id': invite_id,  # Reference to invite that authorized this user
        'signed_by': invite_id,  # Polymorphic signer field (verified with invite_pubkey)
        'user_pubkey': crypto.b64encode(user_pubkey),  # User's OWN public key (for signing first peer invite)
        'peer_id': peer_shared_id,  # References the public peer identity
        'name': name,
        # Note: signed_by is invite_id (above), not peer_shared_id
        'created_at': t_ms
    }

    # Add network_id if present
    if network_id:
        event_data['network_id'] = network_id

    # Phase 2: Sign with invite_private_key (proves possession of invite link)
    # NOT with peer's private key - the signature proves invite possession
    signed_event = crypto.sign_event(event_data, invite_private_key)

    # Store as signed plaintext (no inner encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    user_id = store.event(blob, peer_id, t_ms, db)

    # Auto-create prekey for sync requests (inline, following poc-5 pattern)
    # Create local prekey (local-only, has private key)
    from events.network import transit_prekey
    from events.network import transit_prekey_shared
    prekey_id, prekey_private = transit_prekey.create(
        peer_id=peer_id,
        t_ms=t_ms + 1,  # Slightly later timestamp
        db=db
    )

    # Create shareable transit_prekey_shared (shareable, only public key)
    # Signed plaintext only (no encryption for transit prekeys)
    # Linking happens during projection (event-sourcing principle)
    transit_prekey_shared_id = transit_prekey_shared.create(
        prekey_id=prekey_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms + 2,  # Slightly later than prekey
        db=db
    )

    # Phase 2: Return user_private_key for caller to sign first peer invite
    return user_id, transit_prekey_shared_id, prekey_id, user_private_key


def project(user_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project user event into users table.

    Phase 2: User events are verified using invite_pubkey (not peer_shared pubkey).
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

    # Phase 2: All user events must have invite_id and signed_by=invite_id
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

    if not invite_pubkey_bytes:
        # Neither table nor store has invite - return None, will retry later
        log.warning(f"[USER_PROJECT_EARLY_RETURN] invite_id={invite_id[:20]}... not available yet")
        return None

    # Phase 2: Verify signature using invite_pubkey (proves possession of invite link)
    if not crypto.verify_event(event_data, invite_pubkey_bytes):
        log.warning(f"[USER_PROJECT_EARLY_RETURN] Signature verification failed using invite_pubkey")
        return None

    log.info(f"[USER_PROJECT] Signature verified with invite_pubkey from invites table")

    # Extract user_pubkey from event body (Phase 2: user's OWN unique public key)
    user_pubkey = event_data.get('user_pubkey', '')

    # Get network_id from event or from invite
    network_id = event_data.get('network_id')
    if not network_id:
        # Try to get from invite event blob
        invite_blob = store.get(invite_id, db)
        if invite_blob:
            invite_data = crypto.parse_json(invite_blob)
            network_id = invite_data.get('network_id')

    # Insert into users table with user_pubkey (not invite_pubkey)
    log.warning(f"[USER_PROJECT_INSERT] Inserting user into users table: user_id={user_id[:20]}..., peer_id={event_data['peer_id'][:20]}...")
    safedb.execute(
        """INSERT OR IGNORE INTO users
           (user_id, peer_id, name, network_id, created_at, user_pubkey, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            event_data['peer_id'],
            event_data['name'],
            network_id,
            event_data['created_at'],
            user_pubkey,  # Phase 2: User's OWN public key (for verifying signed_by=user_id)
            recorded_by,
            recorded_at
        )
    )
    log.warning(f"[USER_PROJECT_SUCCESS] User inserted successfully")

    # Note: Admin grants for first_peer are handled explicitly in new_network()
    # after the user is created, not as a side effect here.

    # Add to group_members (all_users group from invite)
    # Phase 5: invite_proof was removed, so we add group membership here for all users
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

    # Mark user event as valid for this peer
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (user_id, recorded_by)
    )

    return user_id

def new_network(name: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Create a new user with their own implicit network.

    Phase 5: Network creator now uses self-invite pattern (same flow as joiners).

    Creates:
    - peer (local + shared)
    - groups (all_users + admins)
    - network event (binds groups)
    - channel (default channel)
    - invite (for self-bootstrapping with first_peer)
    - user (via join() using self-invite)

    Args:
        name: Username/display name
        t_ms: Base timestamp (each event gets incremented)
        db: Database connection

    Returns:
        {
            'peer_id': str,
            'peer_shared_id': str,
            'prekey_id': str,
            'network_id': str,
            'all_users_group_id': str,
            'admins_group_id': str,
            'channel_id': str,
            'user_id': str,
        }
    """
    from events.group import group
    from events.identity import network, invite
    from events.content import channel

    log.info(f"new_network() creating network for '{name}' at t_ms={t_ms} (Phase 5: self-invite pattern)")

    # 1. Create peer (local + shared)
    peer_id, peer_shared_id = peer.create(t_ms=t_ms, db=db)
    log.info(f"new_network() created peer: {peer_id[:20]}..., peer_shared={peer_shared_id[:20]}...")

    # 1b. Phase 5: Create transit prekey early (needed for invite.create())
    # Normally created during user.create(), but we need it before invite
    from events.network import transit_prekey, transit_prekey_shared
    prekey_id, prekey_private = transit_prekey.create(
        peer_id=peer_id,
        t_ms=t_ms + 5,
        db=db
    )
    transit_prekey_shared_id = transit_prekey_shared.create(
        prekey_id=prekey_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms + 6,
        db=db
    )
    log.info(f"new_network() created transit prekey: {prekey_id[:20]}..., shared={transit_prekey_shared_id[:20]}...")

    # 2. Create ALL_USERS group (main group for all users)
    all_users_group_id, all_users_key_id = group.create(
        name=f"{name}",
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms + 10,
        db=db,
        is_main=True  # This is the main group for inviting
    )
    log.info(f"new_network() created all_users group: {all_users_group_id[:20]}...")

    # 3. Create ADMINS group (admin-only group)
    admins_group_id, admins_key_id = group.create(
        name=f"{name} - Admins",
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms + 20,
        db=db,
        is_main=False
    )
    log.info(f"new_network() created admins group: {admins_group_id[:20]}...")

    # 4. Create default channel
    channel_id = channel.create(
        name='general',
        group_id=all_users_group_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        key_id=all_users_key_id,
        t_ms=t_ms + 30,
        db=db,
        is_main=True  # This is the main channel
    )
    log.info(f"new_network() created channel: {channel_id[:20]}...")

    # 5. Create NETWORK event (binds all_users + admins groups)
    # Phase 4: network.create() now returns (network_id, network_private_key)
    network_id, network_private_key = network.create(
        all_users_group_id=all_users_group_id,
        admins_group_id=admins_group_id,
        creator_user_id='',  # Placeholder - will be set by first user
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms + 40,
        db=db
    )
    log.info(f"new_network() created network: {network_id[:20]}...")

    # Phase 5: Bootstrap - manually insert network into networks table
    # This allows invite.create() to query for it before full projection
    # Phase 4: Include network_pubkey for bootstrap invite verification
    from db import create_safe_db
    import store
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get network_pubkey from the event we just created
    network_blob = store.get(network_id, db)
    network_event_data = crypto.parse_json(network_blob)
    network_pubkey = network_event_data.get('network_pubkey', '')

    safedb.execute(
        """INSERT OR IGNORE INTO networks
           (network_id, all_users_group_id, admins_group_id, creator_user_id, network_pubkey, signed_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            network_id,
            all_users_group_id,
            admins_group_id,
            '',  # Placeholder
            network_pubkey,  # Phase 4
            peer_shared_id,  # signed_by
            t_ms + 40,
            peer_id,
            t_ms + 40
        )
    )
    log.info(f"new_network() bootstrap inserted network into networks table")

    # 6. Phase 6: Create bootstrap user invite signed by network_id
    # This replaces the legacy first_peer mechanism
    invite_id, invite_private_key, invite_pubkey = invite.create_bootstrap_user_invite(
        network_id=network_id,
        network_private_key=network_private_key,
        group_id=all_users_group_id,
        channel_id=channel_id,
        key_id=all_users_key_id,
        peer_id=peer_id,
        first_peer=peer_shared_id,  # Grants admin on join
        t_ms=t_ms + 50,
        db=db
    )
    log.info(f"new_network() created bootstrap invite signed by network: {invite_id[:20]}...")

    # Build invite_link for join() - must match the format expected by join()
    # Get the invite blob and peer_shared blob for the link
    import json
    invite_blob = store.get(invite_id, db)
    peer_shared_blob = store.get(peer_shared_id, db)

    # Generate deterministic prekey ID from public key hash (same as invite.create())
    invite_prekey_id = crypto.b64encode(crypto.hash(invite_pubkey)[:16])

    invite_link_data = {
        'invite_blob': base64.urlsafe_b64encode(invite_blob).decode().rstrip('='),
        'invite_id': invite_id,
        'invite_prekey_id': invite_prekey_id,
        'invite_private_key': crypto.b64encode(invite_private_key),
        'inviter_peer_shared_id': peer_shared_id,
        'inviter_peer_shared_blob': base64.urlsafe_b64encode(peer_shared_blob).decode().rstrip('='),
        'first_peer': peer_shared_id,  # For admin grant on join
        'ip': '127.0.0.1',
        'port': 6100,
    }
    invite_json = json.dumps(invite_link_data, separators=(',', ':'), sort_keys=True)
    invite_code = base64.urlsafe_b64encode(invite_json.encode()).decode().rstrip('=')
    invite_link = f"quiet://invite/{invite_code}"
    log.info(f"new_network() built invite_link with invite_prekey_id={invite_prekey_id[:20]}...")

    # 7. Phase 5: Join using own invite (same code path as any joiner!)
    join_result = join(
        peer_id=peer_id,  # Phase 5: Pass existing peer_id
        invite_link=invite_link,
        name=name,
        t_ms=t_ms + 100,
        db=db
    )
    log.info(f"new_network() joined via self-invite: user_id={join_result['user_id'][:20]}...")

    # 8. Grant admin privileges to network creator
    # This is explicit here rather than hidden as a side-effect in user.project()
    from events.group import group_member
    admin_member_id = group_member.create(
        group_id=admins_group_id,
        user_id=join_result['user_id'],
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms + 110,
        db=db,
        skip_admin_check=True  # Bootstrap: first user grants themselves admin
    )
    log.info(f"new_network() granted admin to creator: {admin_member_id[:20]}...")

    db.commit()

    # Return combined result
    return {
        **join_result,
        'network_id': network_id,
        'all_users_group_id': all_users_group_id,
        'admins_group_id': admins_group_id,
        'channel_id': channel_id,
        'invite_id': invite_id,
        # Backward compatibility - group_id and key_id reference all_users group
        'group_id': all_users_group_id,
        'key_id': all_users_key_id,
    }


def join(peer_id: str, invite_link: str, name: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Join an existing network via invite link.

    Phase 5: Peer must be created by caller before calling join().
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

    Returns:
        {
            'peer_id': str,
            'peer_shared_id': str,
            'user_id': str,
            'group_id': str,
            'invite_data': dict,
        }
    """
    log.info(f"join() user '{name}' joining via invite at t_ms={t_ms} with peer_id={peer_id[:20]}...")

    # Phase 5: Get peer_shared_id from existing peer
    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=peer_id)
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row:
        raise ValueError(f"Peer {peer_id} not found. Create peer with peer.create() before calling join().")
    peer_shared_id = peer_self_row['peer_shared_id']

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

    # Extract and store invite event blob (with recorded wrapper for projection)
    invite_blob_b64 = invite_data['invite_blob']
    invite_blob = base64.urlsafe_b64decode(invite_blob_b64 + '===')  # Add padding
    invite_id = store.event(invite_blob, peer_id, t_ms, db)

    # Note: invite is now marked as valid via invite_accepted.project() for reprojection
    # During initial join, we'll mark it valid after creating invite_accepted event below

    # Phase 4: Project inviter's peer_shared FIRST (before invite)
    # This ensures the creator's public key is available when validating the invite signature
    if 'inviter_peer_shared_blob' in invite_data:
        inviter_peer_shared_blob_b64 = invite_data['inviter_peer_shared_blob']
        inviter_peer_shared_blob = base64.urlsafe_b64decode(inviter_peer_shared_blob_b64 + '===')

        # Store the blob and create recorded event
        from events.network import recorded
        unsafedb = create_unsafe_db(db)
        inviter_peer_shared_id = store.blob(inviter_peer_shared_blob, t_ms, return_dupes=True, unsafedb=unsafedb)

        # Create recorded event for this peer
        recorded_id = recorded.create(inviter_peer_shared_id, peer_id, t_ms, db, return_dupes=True)

        # Project it immediately
        recorded.project_ids([recorded_id], db)

        log.info(f"join() projected inviter's peer_shared: {inviter_peer_shared_id[:20]}... for peer {peer_id[:20]}...")

    # Now project invite (after peer_shared, so creator's public key is available for validation)
    from events.identity import invite
    invite.project(invite_id, peer_id, t_ms, db)
    log.info(f"join() projected invite: {invite_id[:20]}...")

    # Extract secrets from invite link (all b64 encoded)
    invite_prekey_id = invite_data['invite_prekey_id']
    invite_private_key = crypto.b64decode(invite_data['invite_private_key'])

    log.info(f"join() extracted invite_prekey_id={invite_prekey_id[:20]}... from invite link")

    # Get metadata from invite event
    invite_event_data = crypto.parse_json(invite_blob)
    group_id = invite_event_data['group_id']
    channel_id = invite_event_data['channel_id']
    key_id = invite_event_data['key_id']

    # Phase 5: Extract first_peer from invite link (for network creator self-bootstrapping)
    first_peer = invite_data.get('first_peer')

    # Create invite_accepted event FIRST to capture ALL invite link data for event-sourcing
    # This restores the invite private key via projection BEFORE user.create() is called
    # This allows reprojection to work without the original invite link
    from events.identity import invite_accepted
    invite_accepted_id = invite_accepted.create(
        invite_id=invite_id,
        invite_prekey_id=invite_prekey_id,
        invite_private_key=invite_private_key,
        peer_id=peer_id,
        t_ms=t_ms + 1,  # Before user creation
        db=db,
        first_peer=first_peer  # Phase 5: Pass first_peer for admin grant
    )

    # 2. Create user membership (auto-creates transit_prekey + transit_prekey_shared)
    # Phase 2: User event is signed by invite (signed_by=invite_id)
    # Returns user_private_key for signing first peer invite (Phase 3)
    user_id, transit_prekey_shared_id, prekey_id, user_private_key = create(
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        name=name,
        t_ms=t_ms + 2,
        db=db,
        invite_id=invite_id,
        invite_private_key=invite_private_key  # Phase 2: Required for signing user event
    )

    # Phase 5: invite_proof removed - proof IS the signature on user event (signed_by=invite_id)

    log.info(f"join() user '{name}' joined: peer={peer_id[:20]}..., group={group_id[:20]}...")

    # Create network_joined event immediately to mark bootstrap intent
    # The inviter_peer_shared_id comes from the invite event
    inviter_peer_shared_id = invite_event_data.get('inviter_peer_shared_id')
    if inviter_peer_shared_id:
        from events.identity import network_joined
        network_joined_id = network_joined.create(
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            inviter_peer_shared_id=inviter_peer_shared_id,
            t_ms=t_ms + 3,  # After user creation
            db=db
        )
        log.info(f"join() created network_joined {network_joined_id[:20]}... for peer {peer_id[:20]}...")
    else:
        log.warning(f"join() invite event missing inviter_peer_shared_id, skipping network_joined creation")

    return {
        'peer_id': peer_id,
        'peer_shared_id': peer_shared_id,
        'user_id': user_id,
        'prekey_id': prekey_id,
        'transit_prekey_shared_id': transit_prekey_shared_id,
        'group_id': group_id,
        'channel_id': channel_id,
        'key_id': key_id,
        'invite_data': invite_data,
        'invite_accepted_id': invite_accepted_id,
        'user_private_key': user_private_key,  # Phase 2: For signing first peer invite (Phase 3)
    }
