"""User event type (shareable, encrypted) - represents network membership."""
from typing import Any
import logging
import crypto
import store
from events.transit import transit_key
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, peer_shared_id: str, name: str, t_ms: int, db: Any,
           invite_id: str | None = None, invite_key_secret: bytes | None = None,
           invite_key_id: str | None = None,
           invite_private_key: bytes | None = None,
           group_id: str | None = None, channel_id: str | None = None) -> tuple[str, str]:
    """Create a user event representing network membership.

    Also auto-creates a prekey for receiving sync requests.

    Two modes:
    1. Invite joiner (Bob): Requires invite_id, invite_key_secret, invite_key_id. Metadata from invite.
    2. Network creator (Alice): Requires group_id, channel_id. No invite.

    Args:
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by and peer_id in event)
        name: Display name for the user
        t_ms: Timestamp
        db: Database connection
        invite_id: Reference to invite event (for invite joiners)
        invite_key_secret: Invite group key bytes for wrapping (for invite joiners)
        invite_key_id: Pre-computed key ID from Alice (for invite joiners)
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
        else:
            raise ValueError(f"invite event not found: {invite_id}")
    else:
        # Network creator - include metadata directly (old format for compatibility)
        event_data['group_id'] = group_id
        event_data['channel_id'] = channel_id

    # If joining via invite, add invite proof fields
    if invite_private_key:
        # Derive public key from private key
        import nacl.signing
        signing_key = nacl.signing.SigningKey(invite_private_key)
        invite_public_key = bytes(signing_key.verify_key)
        invite_pubkey_b64 = crypto.b64encode(invite_public_key)

        # Sign proof message: peer_shared_id + ":" + invite_id
        # This proves Bob knows the invite private key and is joining via this specific invite
        proof_message = f"{peer_shared_id}:{invite_id}".encode('utf-8')
        invite_signature = crypto.sign(proof_message, invite_private_key)
        invite_signature_b64 = crypto.b64encode(invite_signature)

        event_data['invite_pubkey'] = invite_pubkey_b64
        event_data['invite_signature'] = invite_signature_b64

        # Store invite prekey in transit_prekeys so Bob can decrypt invite_key_shared
        # Use the invite prekey_id from invite link (hash of public key)
        invite_prekey_id = crypto.b64encode(crypto.hash(invite_public_key, size=16))
        unsafedb = create_unsafe_db(db)
        unsafedb.execute(
            "INSERT OR REPLACE INTO transit_prekeys (prekey_id, owner_peer_id, public_key, private_key, created_at) VALUES (?, ?, ?, ?, ?)",
            (invite_prekey_id, peer_id, invite_public_key, invite_private_key, t_ms)
        )

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
    # Get blob from store
    blob = store.get(user_id, db)
    if not blob:
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
        log.debug(f"user.project() user_id={user_id} skipping - peer_shared {created_by} not available yet")
        return None

    if not crypto.verify_event(event_data, public_key):
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
            log.debug(f"user.project() user_id={user_id} skipping - invite_id={invite_id} not in store yet")
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

        # Validate invite proof if present (Bob proving he has invite private key)
        if 'invite_pubkey' in event_data:
            if not event_data.get('invite_signature'):
                return None  # Missing signature

            # Verify invite_pubkey matches invite event
            if event_data['invite_pubkey'] != invite_data['invite_pubkey']:
                log.warning(f"user.project() invite_pubkey mismatch")
                return None

            # Verify the Ed25519 signature: peer_id + ":" + invite_id
            proof_message = f"{event_data['peer_id']}:{invite_id}".encode('utf-8')
            invite_pubkey = crypto.b64decode(event_data['invite_pubkey'])
            invite_signature = crypto.b64decode(event_data['invite_signature'])

            if not crypto.verify(proof_message, invite_signature, invite_pubkey):
                log.warning(f"user.project() invite proof signature verification failed")
                return None

    # Insert into users table
    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO users
           (user_id, peer_id, name, joined_at, invite_pubkey, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            event_data['peer_id'],
            event_data['name'],
            event_data['created_at'],
            event_data.get('invite_pubkey', ''),
            recorded_by,
            recorded_at
        )
    )

    # Insert into group_members table
    safedb.execute(
        """INSERT OR IGNORE INTO group_members
           (group_id, user_id, added_by, added_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            group_id,  # Extracted from invite event (or event_data for network creator)
            user_id,
            event_data['created_by'],  # Self-added for invite joins
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


def send_bootstrap_events(peer_id: str, peer_shared_id: str, user_id: str,
                          transit_prekey_shared_id: str, invite_data: dict[str, Any],
                          t_ms: int, db: Any) -> None:
    """Send peer, user, address, transit_prekey_shared events to invite address for bootstrap.

    Called repeatedly until sync is established (job pattern).
    Uses invite transit key from invite_data for outer encryption.

    SECURITY: This function trusts that peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function. This is safe for local-only apps where
    the user controls all peers on the device.

    Args:
        peer_id: Local peer ID (private)
        peer_shared_id: Public peer ID
        user_id: User event ID
        transit_prekey_shared_id: transit_prekey_shared event ID (shareable transit prekey)
        invite_data: Decoded invite link data containing invite_transit_key_secret, ip, port
        t_ms: Timestamp
        db: Database connection
    """
    import queues
    from db import create_unsafe_db

    unsafedb = create_unsafe_db(db)

    # Get invite address from invite_data (parsed from link)
    invite_ip = invite_data.get('ip', '127.0.0.1')
    invite_port = invite_data.get('port', 6100)
    invite_transit_key_secret = crypto.b64decode(invite_data['invite_transit_key'])
    invite_transit_key_id = invite_data['invite_transit_key_id']  # Alice's pre-computed ID

    # Create invite transit key dict for outer wrapping (use Alice's pre-computed ID)
    import logging
    log = logging.getLogger(__name__)
    log.info(f"send_bootstrap_events() using invite_transit_key_id={invite_transit_key_id}")

    invite_transit_key = {
        'id': crypto.b64decode(invite_transit_key_id),
        'key': invite_transit_key_secret,
        'type': 'symmetric'
    }

    # Get peer_shared blob
    peer_shared_blob = store.get(peer_shared_id, unsafedb)
    if not peer_shared_blob:
        return  # Can't send if blob not found

    # Wrap with invite transit key for outer encryption
    wrapped_peer = crypto.wrap(peer_shared_blob, invite_transit_key, db)

    # Get user blob
    user_blob = store.get(user_id, unsafedb)
    if not user_blob:
        return  # Can't send if blob not found

    # Wrap with invite transit key for outer encryption
    wrapped_user = crypto.wrap(user_blob, invite_transit_key, db)

    # Create address event (plaintext, will be wrapped)
    # TODO: Get actual address from somewhere instead of hardcoding
    address_data = {
        'type': 'address',
        'peer_id': peer_shared_id,
        'ip': '127.0.0.1',  # Bob's address (hardcoded for now)
        'port': 6100,
        'created_at': t_ms
    }

    # Sign and wrap address event
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_address = crypto.sign_event(address_data, private_key)
    canonical_address = crypto.canonicalize_json(signed_address)
    wrapped_address = crypto.wrap(canonical_address, invite_transit_key, db)

    # Get transit_prekey_shared blob (created by user.create() auto-creation)
    transit_prekey_shared_blob = store.get(transit_prekey_shared_id, unsafedb)
    if not transit_prekey_shared_blob:
        return  # Can't send if blob not found

    # Wrap with invite transit key for outer encryption
    wrapped_transit_prekey_shared = crypto.wrap(transit_prekey_shared_blob, invite_transit_key, db)

    # Add all to incoming queue (simulates sending to invite address)
    inviter_id_short = invite_data.get('inviter_peer_shared_id', 'N/A')[:10] + '...' if invite_data.get('inviter_peer_shared_id') else 'N/A'
    log.warning(f"[BOOTSTRAP_SEND] from_peer={peer_id[:10]}... to_inviter={inviter_id_short} invite_transit_key_id={invite_transit_key_id[:20]}... sending_4_events")
    queues.incoming.add(wrapped_peer, t_ms, db)
    log.warning(f"[BOOTSTRAP_SEND] event=peer_shared blob_size={len(wrapped_peer)}B hint={crypto.b64encode(wrapped_peer[:16])[:20]}...")
    queues.incoming.add(wrapped_user, t_ms, db)
    log.warning(f"[BOOTSTRAP_SEND] event=user blob_size={len(wrapped_user)}B hint={crypto.b64encode(wrapped_user[:16])[:20]}...")
    queues.incoming.add(wrapped_address, t_ms, db)
    log.warning(f"[BOOTSTRAP_SEND] event=address blob_size={len(wrapped_address)}B hint={crypto.b64encode(wrapped_address[:16])[:20]}...")
    queues.incoming.add(wrapped_transit_prekey_shared, t_ms, db)
    log.warning(f"[BOOTSTRAP_SEND] event=transit_prekey_shared blob_size={len(wrapped_transit_prekey_shared)}B hint={crypto.b64encode(wrapped_transit_prekey_shared[:16])[:20]}...")

    # Send sync request to inviter (Bob initiates sync with Alice)
    # This allows Alice to send her events back to Bob in the sync response
    inviter_peer_shared_id = invite_data.get('inviter_peer_shared_id')
    if inviter_peer_shared_id:
        from events.transit import sync
        # inviter_peer_shared_id is the inviter's public identity
        sync.send_request(
            to_peer_shared_id=inviter_peer_shared_id,
            from_peer_id=peer_id,
            from_peer_shared_id=peer_shared_id,
            t_ms=t_ms,
            db=db
        )


def new_network(name: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Create a new user with their own implicit network.

    Creates:
    - peer (local + shared)
    - prekey (local + shared)
    - key (symmetric, for the network)
    - group (serves as the network)
    - channel (default channel)
    - user (membership record)

    Args:
        name: Username/display name
        t_ms: Base timestamp (each event gets incremented)
        db: Database connection

    Returns:
        {
            'peer_id': str,
            'peer_shared_id': str,
            'prekey_id': str,
            'key_id': str,
            'group_id': str,  # Also serves as network_id
            'channel_id': str,
            'user_id': str,
            'invite_link': str,
        }
    """
    from events.transit import transit_prekey
    from events.group import group
    from events.identity import invite
    from events.content import channel

    log.info(f"new_network() creating user '{name}' at t_ms={t_ms}")

    # 1. Create peer (local + shared)
    peer_id, peer_shared_id = peer.create(t_ms=t_ms, db=db)

    # 2. Create group (group creates its own encryption key)
    # Mark as main group for inviting
    group_id, key_id = group.create(
        name=f"{name}'s Network",
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms + 1,
        db=db,
        is_main=True  # This is the main group for inviting
    )

    # 3. Share the group key with ourselves (sealed to our own prekey)
    # This ensures we have a decryptable copy that syncs to other peers
    from events.group import group_key_shared
    from events.transit import transit_prekey

    try:
        our_prekey = transit_prekey.get_transit_prekey_for_peer(peer_shared_id, peer_id, db)
        if our_prekey:
            group_key_shared.create(
                key_id=key_id,  # Group's key we just created
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_peer_id=peer_shared_id,  # Sealed to ourselves
                t_ms=t_ms + 2,
                db=db
            )
            log.info(f"new_network() created group_key_shared for creator {peer_shared_id[:20]}...")
        else:
            log.warning(f"new_network() no prekey found for creator {peer_shared_id[:20]}...")
    except Exception as e:
        log.warning(f"new_network() failed to create group_key_shared: {e}")

    # 4. Create default channel
    # Mark as main channel
    channel_id = channel.create(
        name='general',
        group_id=group_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        key_id=key_id,
        t_ms=t_ms + 3,
        db=db,
        is_main=True  # This is the main channel
    )

    # 5. Create user membership record (auto-creates transit_prekey + transit_prekey_shared)
    # Network creator - no invite
    user_id, transit_prekey_shared_id, prekey_id = create(
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        name=name,
        t_ms=t_ms + 4,
        db=db,
        group_id=group_id,
        channel_id=channel_id
    )

    log.info(f"new_network() created user '{name}': peer={peer_id}, group={group_id}")

    # 6. Create network_created event to mark this peer as network creator (self-bootstrapped)
    network_created_event = {
        'type': 'network_created',
        'peer_id': peer_id,
        'peer_shared_id': peer_shared_id,
        'group_id': group_id,
        'created_by': peer_shared_id,
        'created_at': t_ms + 5
    }

    # Sign and store the event
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(network_created_event, private_key)
    network_created_blob = crypto.canonicalize_json(signed_event)
    network_created_id = store.event(network_created_blob, peer_id, t_ms + 5, db)

    log.info(f"new_network() created network_created event: {network_created_id[:20]}...")

    return {
        'peer_id': peer_id,
        'peer_shared_id': peer_shared_id,
        'prekey_id': prekey_id,
        'transit_prekey_shared_id': transit_prekey_shared_id,
        'key_id': key_id,
        'group_id': group_id,
        'channel_id': channel_id,
        'user_id': user_id,
        'network_created_id': network_created_id,
    }


def join(invite_link: str, name: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Join an existing network via invite link.

    Creates:
    - peer (local + shared)
    - user (membership with invite proof, auto-creates prekey + prekey_shared)

    The inviter will share:
    - key (via key_shared event)
    - group, channel events (via sync)

    Args:
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
    log.info(f"join() user '{name}' joining via invite at t_ms={t_ms}")

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

    # 1. Create peer (local + shared) - must be first for invite storage
    peer_id, peer_shared_id = peer.create(t_ms=t_ms, db=db)

    # Extract and store invite event blob (with recorded wrapper for projection)
    invite_blob_b64 = invite_data['invite_blob']
    invite_blob = base64.urlsafe_b64decode(invite_blob_b64 + '===')  # Add padding
    invite_id = store.event(invite_blob, peer_id, t_ms, db)

    # Mark invite as valid immediately (out-of-band trust via URL)
    # This bypasses dependency checking since we trust the invite link itself
    safedb = create_safe_db(db, recorded_by=peer_id)
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_id, peer_id)
    )

    # Project invite immediately to restore invite_key_secret and invite prekey to prekeys_shared
    # The invite prekey is reconstructed from invite event data (doesn't require decryption)
    from events.identity import invite
    invite.project(invite_id, peer_id, t_ms, db)

    # Project inviter's peer_shared from invite link (allows immediate sync with inviter)
    # This puts the inviter in joinee's peers_shared table before bootstrap starts
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

    # Extract secrets from invite link (all b64 encoded)
    invite_private_key = crypto.b64decode(invite_data['invite_private_key'])

    # Extract keys and their pre-computed IDs from invite link
    invite_transit_key = crypto.b64decode(invite_data['invite_transit_key'])
    invite_transit_key_id = invite_data['invite_transit_key_id']  # Alice's pre-computed ID

    invite_group_key = crypto.b64decode(invite_data['invite_group_key'])
    invite_group_key_id = invite_data['invite_group_key_id']  # Alice's pre-computed ID

    log.info(f"join() extracted invite_transit_key_id={invite_transit_key_id} from invite link")
    log.info(f"join() extracted invite_group_key_id={invite_group_key_id} from invite link")

    # Get metadata from invite event
    invite_event_data = crypto.parse_json(invite_blob)
    group_id = invite_event_data['group_id']
    channel_id = invite_event_data['channel_id']
    key_id = invite_event_data['key_id']

    # 2. Create user membership with invite proof (auto-creates transit_prekey + transit_prekey_shared)
    # User event references invite_id (contains group/channel/key metadata)
    user_id, transit_prekey_shared_id, prekey_id = create(
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        name=name,
        t_ms=t_ms + 1,
        db=db,
        invite_id=invite_id,
        invite_key_secret=invite_group_key,  # Inner encryption for bootstrap events
        invite_key_id=invite_group_key_id,  # Alice's pre-computed ID
        invite_private_key=invite_private_key
    )

    log.info(f"join() user '{name}' joined: peer={peer_id}, group={group_id}")

    # Get invite_transit_prekey_shared_id from projected invite event
    invite_transit_prekey_shared_id = invite_event_data.get('invite_transit_prekey_shared_id')
    if not invite_transit_prekey_shared_id:
        raise ValueError("invite_transit_prekey_shared_id missing from invite event")

    # Create invite_accepted event to capture ALL invite link data for event-sourcing
    # This allows reprojection to work without the original invite link
    from events.identity import invite_accepted
    invite_accepted_id = invite_accepted.create(
        invite_id=invite_id,
        invite_private_key=invite_private_key,
        invite_transit_prekey_shared_id=invite_transit_prekey_shared_id,
        invite_transit_key=invite_transit_key,
        invite_transit_key_id=invite_transit_key_id,  # Alice's pre-computed ID
        invite_group_key=invite_group_key,
        invite_group_key_id=invite_group_key_id,  # Alice's pre-computed ID
        peer_id=peer_id,
        t_ms=t_ms + 2,  # After user creation
        db=db
    )

    # Add invite_transit_key_id to invite_data for send_bootstrap_events
    invite_data['invite_transit_key_id'] = invite_transit_key_id

    # TODO: Create network_joined event after receiving first sync response from inviter
    # This marks bootstrap as successful and switches from bootstrap mode to normal sync

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
