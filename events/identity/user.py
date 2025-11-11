"""User event type (shareable, encrypted) - represents network membership."""
from typing import Any
import base64
import logging
import crypto
import store
from events.transit import transit_key
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, peer_shared_id: str, name: str, t_ms: int, db: Any,
           invite_id: str | None = None,
           invite_private_key: bytes | None = None,
           group_id: str | None = None, channel_id: str | None = None,
           network_id: str | None = None) -> tuple[str, str, str]:
    """Create a user event representing network membership.

    Also auto-creates a prekey for receiving sync requests.

    Two modes:
    1. Invite joiner (Bob): Requires invite_id, invite_private_key. Metadata from invite.
    2. Network creator (Alice): Requires group_id, channel_id. No invite.

    Args:
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by and peer_id in event)
        name: Display name for the user
        t_ms: Timestamp
        db: Database connection
        invite_id: Reference to invite event (for invite joiners)
        invite_private_key: Invite private key for proof (for invite joiners)
        group_id: Group ID (for network creators only)
        channel_id: Channel ID (for network creators only)

    Returns:
        (user_id, transit_prekey_shared_id, transit_prekey_id): The stored user, transit_prekey_shared and transit_prekey event IDs
    """
    # Create base user event
    event_data = {
        'type': 'user',
        'peer_id': peer_shared_id,  # References the public peer identity
        'name': name,
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

    # Add invite_id if joining via invite, or metadata if network creator
    if invite_id:
        event_data['invite_id'] = invite_id  # Reference to invite event

        # Extract metadata from invite for prekey_shared creation
        invite_blob = store.get(invite_id, db)
        if invite_blob:
            invite_event_data = crypto.parse_json(invite_blob)
            group_id = invite_event_data['group_id']
            channel_id = invite_event_data['channel_id']
            key_id = invite_event_data['key_id']
            network_id = invite_event_data.get('network_id')  # NEW - extract from invite
        else:
            raise ValueError(f"invite event not found: {invite_id}")
    else:
        # Network creator - include metadata directly (old format for compatibility)
        event_data['group_id'] = group_id
        event_data['channel_id'] = channel_id
        # network_id passed as parameter for network creator

    # Add network_id if present (joiners get from invite, creators pass explicitly)
    if network_id:
        event_data['network_id'] = network_id

    # Note: Invite proof is now created as a separate invite_proof event
    # (removed from user event to decouple invite validation)

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext (no inner encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    user_id = store.event(blob, peer_id, t_ms, db)

    # Auto-create prekey for sync requests (inline, following poc-5 pattern)
    # Create local prekey (local-only, has private key)
    from events.transit import transit_prekey
    from events.transit import transit_prekey_shared
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

    return user_id, transit_prekey_shared_id, prekey_id


def project(user_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project user event into users and group_members tables."""
    log.warning(f"[USER_PROJECT_ENTRY] user.project() called: user_id={user_id[:20]}..., recorded_by={recorded_by[:20]}...")

    # Get blob from store
    blob = store.get(user_id, db)
    if not blob:
        log.warning(f"[USER_PROJECT_EARLY_RETURN] Blob not found for user_id={user_id[:20]}...")
        return None

    # Parse JSON (signed plaintext, no decryption needed)
    event_data = crypto.parse_json(blob)

    # Verify signature - get public key from created_by peer_shared
    from events.identity import peer_shared
    created_by = event_data['created_by']
    try:
        public_key = peer_shared.get_public_key(created_by, recorded_by, db)
    except ValueError:
        # peer_shared not projected yet - return None without blocking
        # (peer_shared_id is not an event_id, so we can't block on it directly)
        # The recorded.project() will handle crypto dependencies via unwrap()
        log.warning(f"[USER_PROJECT_EARLY_RETURN] peer_shared {created_by[:20]}... not available yet")
        return None

    if not crypto.verify_event(event_data, public_key):
        log.warning(f"[USER_PROJECT_EARLY_RETURN] Signature verification failed")
        return None

    # Fetch invite event and extract metadata
    invite_id = event_data.get('invite_id')
    if not invite_id:
        # Network creator (Alice) doesn't have invite_id
        # Extract from event_data (old format compatibility)
        group_id = event_data.get('group_id')
        channel_id = event_data.get('channel_id')
        key_id = event_data.get('key_id')
    else:
        # Fetch invite event blob
        invite_blob = store.get(invite_id, db)
        if not invite_blob:
            # invite not projected yet - return None, will retry later
            log.warning(f"[USER_PROJECT_EARLY_RETURN] invite_id={invite_id[:20]}... not in store yet")
            return None

        # Parse invite event (plaintext JSON, not encrypted)
        invite_data = crypto.parse_json(invite_blob)

        # Note: Invite signature verification skipped
        # Invite was trusted when stored (via URL for joiners, local creation for inviters)
        # Signature is in the blob for future verification if needed

        # Extract metadata from invite
        group_id = invite_data['group_id']
        channel_id = invite_data['channel_id']
        key_id = invite_data['key_id']

        # Note: Invite proof validation is now handled by separate invite_proof event
        # (removed from user.project() to decouple invite validation)

    # Insert into users table
    safedb = create_safe_db(db, recorded_by=recorded_by)
    network_id = event_data.get('network_id')  # NEW - may be from invite or event_data
    log.warning(f"[USER_PROJECT_INSERT] Inserting user into users table: user_id={user_id[:20]}..., peer_id={event_data['peer_id'][:20]}...")
    safedb.execute(
        """INSERT OR IGNORE INTO users
           (user_id, peer_id, name, network_id, created_at, invite_pubkey, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            event_data['peer_id'],
            event_data['name'],
            network_id,  # NEW
            event_data['created_at'],
            event_data.get('invite_pubkey', ''),
            recorded_by,
            recorded_at
        )
    )
    log.warning(f"[USER_PROJECT_SUCCESS] User inserted successfully")

    # Phase 5: Check if this user is first_peer (network creator via self-invite)
    # If so, grant admin privileges by adding to admins group
    if invite_id:
        # Check if this invite has first_peer field
        invite_blob = store.get(invite_id, db)
        log.warning(f"[FIRST_PEER_CHECK] invite_id={invite_id[:20]}..., invite_blob exists={invite_blob is not None}")
        if invite_blob:
            invite_data = crypto.parse_json(invite_blob)
            first_peer = invite_data.get('first_peer')
            log.warning(f"[FIRST_PEER_CHECK] first_peer={first_peer[:20] if first_peer else 'None'}..., event_data peer_id={event_data['peer_id'][:20]}...")
            if first_peer:
                # Check if this user's peer_id matches first_peer
                if event_data['peer_id'] == first_peer:
                    log.warning(f"[FIRST_PEER_MATCH] user.project() detected first_peer user: {user_id[:20]}...")
                    # Grant admin by adding to admins group
                    # Get network's admin group
                    network_row = safedb.query_one(
                        "SELECT admins_group_id FROM networks WHERE recorded_by = ? LIMIT 1",
                        (recorded_by,)
                    )
                    log.warning(f"[FIRST_PEER_GRANT] network_row={network_row}")
                    if network_row and network_row['admins_group_id']:
                        admins_group_id = network_row['admins_group_id']

                        # Create a group_member EVENT so it syncs to other peers
                        from events.group import group_member
                        try:
                            member_event_id = group_member.create(
                                group_id=admins_group_id,
                                user_id=user_id,
                                peer_id=recorded_by,  # Added by self (local peer_id)
                                peer_shared_id=event_data['peer_id'],  # Creator's public peer_shared_id
                                t_ms=recorded_at,
                                db=db
                            )
                            log.warning(f"[FIRST_PEER_GRANTED] user.project() granted first_peer admin via event: {member_event_id[:20]}...")
                        except Exception as e:
                            log.warning(f"[FIRST_PEER_GRANT_ERROR] Failed to create admin member event: {e}")

    # Add to group_members
    # For invite joiners, this is handled by invite_proof.project()
    # For network creators (no invite_id), we add directly here
    if not invite_id:
        # Network creator - add to group directly
        safedb.execute(
            """INSERT OR IGNORE INTO group_members
               (member_id, group_id, user_id, added_by, created_at, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,  # Use user_id as member_id for bootstrap membership
                group_id,  # From event_data for network creator
                user_id,
                event_data['created_by'],  # Self-added
                event_data['created_at'],
                recorded_by,
                recorded_at
            )
        )

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
    from events.transit import transit_prekey, transit_prekey_shared
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
    # Note: creator_user_id will be validated after user.join() completes
    network_id = network.create(
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
    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=peer_id)
    safedb.execute(
        """INSERT OR IGNORE INTO networks
           (network_id, all_users_group_id, admins_group_id, creator_user_id, created_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            network_id,
            all_users_group_id,
            admins_group_id,
            '',  # Placeholder
            peer_shared_id,
            t_ms + 40,
            peer_id,
            t_ms + 40
        )
    )
    log.info(f"new_network() bootstrap inserted network into networks table")

    # 6. Phase 5: Create invite for self-bootstrapping with first_peer
    # This invite will have first_peer=peer_shared_id, granting admin on join
    invite_id, invite_link, invite_data = invite.create(
        peer_id=peer_id,
        t_ms=t_ms + 50,
        db=db,
        mode='user',
        first_peer=peer_shared_id  # THIS IS THE KEY! Grants admin on join
    )
    log.info(f"new_network() created self-invite with first_peer: {invite_id[:20]}...")

    # 7. Phase 5: Join using own invite (same code path as any joiner!)
    join_result = join(
        peer_id=peer_id,  # Phase 5: Pass existing peer_id
        invite_link=invite_link,
        name=name,
        t_ms=t_ms + 100,
        db=db
    )
    log.info(f"new_network() joined via self-invite: user_id={join_result['user_id'][:20]}...")

    # Phase 5: Bootstrap cascade is triggered automatically when invite_accepted is projected
    # by store.event() in user.join(). No manual projection needed.

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
        from events.transit import recorded
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
    # User event references invite_id (contains group/channel/key metadata)
    # Invite proof is created separately below
    user_id, transit_prekey_shared_id, prekey_id = create(
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        name=name,
        t_ms=t_ms + 2,
        db=db,
        invite_id=invite_id,
        invite_private_key=None  # No longer embedded in user event
    )

    # 3. Create separate invite_proof event
    from events.identity import invite_proof
    invite_proof_id = invite_proof.create(
        invite_id=invite_id,
        mode='user',
        joiner_peer_shared_id=peer_shared_id,
        user_id=user_id,
        link_user_id=None,
        invite_private_key=invite_private_key,
        peer_id=peer_id,
        t_ms=t_ms + 3,  # After user creation
        db=db
    )

    log.info(f"join() user '{name}' joined: peer={peer_id}, group={group_id}, invite_proof={invite_proof_id[:20]}...")

    # Create network_joined event immediately to mark bootstrap intent
    # The inviter_peer_shared_id comes from the invite event
    inviter_peer_shared_id = invite_event_data.get('inviter_peer_shared_id')
    if inviter_peer_shared_id:
        from events.identity import network_joined
        network_joined_id = network_joined.create(
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            inviter_peer_shared_id=inviter_peer_shared_id,
            t_ms=t_ms + 4,  # After invite_proof creation
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
    }
