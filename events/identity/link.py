"""Link event type (shareable) - represents a device linked to a user."""
from typing import Any
import base64
import json
import logging
import crypto
import store
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, peer_shared_id: str, link_invite_id: str,
           link_private_key: bytes, user_id: str, t_ms: int, db: Any) -> tuple[str, str, str]:
    """Create a link event proving device ownership via link_invite.

    Also auto-creates a prekey for receiving sync requests (same as user.create).

    Args:
        peer_id: New device's local peer ID
        peer_shared_id: New device's public peer ID
        link_invite_id: Reference to link_invite event
        link_private_key: Private key from link URL (proves access to link_invite)
        user_id: User being linked to
        t_ms: Timestamp
        db: Database connection

    Returns:
        (link_id, transit_prekey_shared_id, transit_prekey_id): The link event ID and prekey IDs
    """
    log.info(f"link.create() for user_id={user_id}, peer_id={peer_id}")

    # Create proof signature proving we have the link_private_key
    # Sign: peer_shared_id + ":" + user_id
    proof_message = f"{peer_shared_id}:{user_id}".encode('utf-8')
    link_signature = crypto.sign(proof_message, link_private_key)
    link_signature_b64 = crypto.b64encode(link_signature)

    # Derive public key from private key for verification
    import nacl.signing
    signing_key = nacl.signing.SigningKey(link_private_key)
    link_public_key = bytes(signing_key.verify_key)
    link_pubkey_b64 = crypto.b64encode(link_public_key)

    # Create link event (shareable, signed by new peer)
    link_event_data = {
        'type': 'link',
        'link_invite_id': link_invite_id,  # Reference to the link_invite
        'link_pubkey': link_pubkey_b64,  # Public key matching link_invite.link_pubkey
        'link_signature': link_signature_b64,  # Proof of ownership
        'user_id': user_id,  # User being linked to
        'peer_id': peer_shared_id,  # New device's peer
        'signed_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign the link event with new peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_link_event = crypto.sign_event(link_event_data, private_key)

    # Store as shareable event (will be synced to network)
    link_blob = crypto.canonicalize_json(signed_link_event)
    link_id = store.event(link_blob, peer_id, t_ms, db)

    log.info(f"link.create() stored link_id={link_id[:20]}...")

    # Update peer_self with our user_id - this is the canonical source for "what user am I?"
    safedb = create_safe_db(db, recorded_by=peer_id)
    safedb.execute(
        "UPDATE peer_self SET user_id = ? WHERE peer_id = ? AND recorded_by = ?",
        (user_id, peer_id, peer_id)
    )
    log.info(f"link.create() set user_id in peer_self")

    # Auto-create prekey for sync requests (same as user.create)
    from events.network import transit_prekey
    from events.network import transit_prekey_shared

    prekey_id, prekey_private = transit_prekey.create(
        peer_id=peer_id,
        t_ms=t_ms + 1,
        db=db
    )

    transit_prekey_shared_id = transit_prekey_shared.create(
        prekey_id=prekey_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms + 2,
        db=db
    )

    log.info(f"link.create() created transit prekey_shared {transit_prekey_shared_id[:20]}...")

    return link_id, transit_prekey_shared_id, prekey_id


def project(link_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project link event into linked_peers and users tables."""
    print(f"[DEBUG] link.project() called: link_id={link_id[:20]}..., recorded_by={recorded_by[:20]}...")
    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(link_id, unsafedb)
    if not blob:
        print(f"[DEBUG] link.project() blob not found for {link_id[:20]}...")
        return None

    # Parse JSON (plaintext, no unwrap needed)
    event_data = crypto.parse_json(blob)

    signed_by = event_data['signed_by']

    log.info(f"link.project() link_id={link_id[:20]}..., signed_by={signed_by[:20]}...")

    # Verify signer (signed_by) exists
    from events.identity import peer_shared
    creator_public_key = peer_shared.get_public_key(signed_by, recorded_by, db)
    if not creator_public_key:
        log.warning(f"link.project() signer not found: {signed_by[:20]}...")
        return None

    # Verify link event signature
    if not crypto.verify_event(event_data, creator_public_key):
        log.warning(f"link.project() signature verification FAILED")
        return None

    # Get the link_invite event
    link_invite_id = event_data['link_invite_id']
    link_invite_blob = store.get(link_invite_id, unsafedb)
    if not link_invite_blob:
        log.warning(f"link.project() link_invite not found: {link_invite_id[:20]}...")
        return None

    link_invite_event_data = crypto.parse_json(link_invite_blob)

    # Verify link_pubkey matches link_invite_pubkey
    # Support both unified format (invite_pubkey) and legacy (link_pubkey)
    invite_pubkey = link_invite_event_data.get('invite_pubkey') or link_invite_event_data.get('link_pubkey')
    if event_data['link_pubkey'] != invite_pubkey:
        log.warning(f"link.project() link_pubkey mismatch - invalid link")
        return None

    # Verify link_invite hasn't expired
    expiry_ms = link_invite_event_data.get('expiry_ms', recorded_at + 86400000)
    if recorded_at > expiry_ms:
        log.warning(f"link.project() link_invite EXPIRED: recorded_at={recorded_at} > expiry_ms={expiry_ms}")
        return None

    # Verify link proof signature: sign(peer_id + ":" + user_id) with link_pubkey
    user_id = event_data['user_id']
    proof_message = f"{signed_by}:{user_id}".encode('utf-8')
    link_pubkey = crypto.b64decode(event_data['link_pubkey'])
    link_signature = crypto.b64decode(event_data['link_signature'])

    if not crypto.verify(proof_message, link_signature, link_pubkey):
        log.warning(f"link.project() proof signature verification FAILED")
        return None

    log.warning(f"link.project() validation passed - link is valid")

    # Insert into linked_peers table (NOT users - linked devices look up user_id via linked_peers)
    safedb.execute(
        """INSERT OR IGNORE INTO linked_peers
           (link_id, user_id, peer_id, linked_at, recorded_by)
           VALUES (?, ?, ?, ?, ?)""",
        (
            link_id,
            user_id,
            signed_by,  # New device's peer_shared_id
            event_data['created_at'],
            recorded_by
        )
    )

    # Mark link as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (link_id, recorded_by)
    )

    log.info(f"link.project() projected link event for user {user_id[:20]}...")
    return link_id


def join(link_url: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Join/link an existing user account via link URL.

    Creates new peer and links it to existing user account.

    Args:
        link_url: Link URL from existing device
        t_ms: Base timestamp
        db: Database connection

    Returns:
        {
            'peer_id': str,
            'peer_shared_id': str,
            'user_id': str,
            'network_id': str,
        }
    """
    log.info(f"link.join() at t_ms={t_ms}")

    # Parse link URL
    if not link_url.startswith('quiet://link/'):
        raise ValueError(f"Invalid link URL format: {link_url}")

    link_code = link_url.replace('quiet://link/', '')
    # Add back padding
    padding = (4 - len(link_code) % 4) % 4
    link_code_padded = link_code + ('=' * padding)

    try:
        link_json = base64.urlsafe_b64decode(link_code_padded).decode()
        link_data = json.loads(link_json)
    except Exception as e:
        raise ValueError(f"Failed to decode link URL: {e}")

    # 1. Create new local peer
    peer_id = peer.create(t_ms=t_ms, db=db)
    log.info(f"link.join() created new local peer: {peer_id[:20]}...")

    # peer_shared_id will be created later after we have the link_invite info

    # Phase: Project network event FIRST if present in link URL
    # The network blob is the root of the dependency cascade:
    # network_id -> bootstrap_invite -> user
    # Without network being valid, user events can't project (they depend on invite, which depends on network)
    if 'network_blob' in link_data:
        network_blob_b64 = link_data['network_blob']
        network_blob = base64.urlsafe_b64decode(network_blob_b64 + '===')

        from events.network import recorded
        unsafedb = create_unsafe_db(db)
        network_id_from_blob = store.blob(network_blob, t_ms, return_dupes=True, unsafedb=unsafedb)

        # Create recorded event for this peer
        recorded_id_network = recorded.create(network_id_from_blob, peer_id, t_ms, db, return_dupes=True)

        # Project it immediately - this marks network_id as valid
        recorded.project_ids([recorded_id_network], db)

        log.info(f"link.join() projected network event: {network_id_from_blob[:20]}...")

    # Extract and store invite/link_invite event blob
    # Support both unified invite format (invite_blob) and legacy (link_invite_blob)
    if 'invite_blob' in link_data:
        # Unified invite format (mode='link')
        invite_blob_b64 = link_data['invite_blob']
        invite_blob = base64.urlsafe_b64decode(invite_blob_b64 + '===')
        link_invite_id = store.event(invite_blob, peer_id, t_ms, db)
        log.info(f"link.join() stored unified invite (mode='link') id={link_invite_id[:20]}...")
    else:
        # Legacy link_invite format
        link_invite_blob_b64 = link_data['link_invite_blob']
        link_invite_blob = base64.urlsafe_b64decode(link_invite_blob_b64 + '===')
        link_invite_id = store.event(link_invite_blob, peer_id, t_ms, db)
        log.info(f"link.join() stored link_invite_id={link_invite_id[:20]}...")

    # Phase 4: Project existing device's peer_shared FIRST (before link_invite)
    # This ensures the creator's public key is available when validating the link_invite signature
    # Support both unified format (inviter_peer_shared_blob) and legacy (existing_peer_shared_blob)
    peer_shared_blob_key = 'inviter_peer_shared_blob' if 'invite_blob' in link_data else 'existing_peer_shared_blob'
    if peer_shared_blob_key in link_data:
        existing_peer_shared_blob_b64 = link_data[peer_shared_blob_key]
        existing_peer_shared_blob = base64.urlsafe_b64decode(existing_peer_shared_blob_b64 + '===')

        from events.network import recorded
        unsafedb = create_unsafe_db(db)
        existing_peer_shared_id = store.blob(existing_peer_shared_blob, t_ms, return_dupes=True, unsafedb=unsafedb)

        # Create recorded event for this peer
        recorded_id = recorded.create(existing_peer_shared_id, peer_id, t_ms, db, return_dupes=True)

        # Project it immediately
        recorded.project_ids([recorded_id], db)

        log.info(f"link.join() projected existing device's peer_shared: {existing_peer_shared_id[:20]}...")

    # Now project link_invite (after peer_shared, so creator's public key is available for validation)
    from events.identity import link_invite
    link_invite.project(link_invite_id, peer_id, t_ms, db)
    log.info(f"link.join() projected link_invite: {link_invite_id[:20]}...")

    # Project existing device's user event from link URL
    if 'existing_user_blob' in link_data:
        existing_user_blob_b64 = link_data['existing_user_blob']
        existing_user_blob = base64.urlsafe_b64decode(existing_user_blob_b64 + '===')

        from events.network import recorded
        unsafedb = create_unsafe_db(db)
        existing_user_id = store.blob(existing_user_blob, t_ms, return_dupes=True, unsafedb=unsafedb)

        # Create recorded event for this peer
        recorded_id_user = recorded.create(existing_user_id, peer_id, t_ms + 1, db, return_dupes=True)

        # Project it immediately
        recorded.project_ids([recorded_id_user], db)

        log.info(f"link.join() projected existing device's user event: {existing_user_id[:20]}...")

    # Extract secrets from link URL
    # For unified format, get invite_prekey_id and invite_private_key
    # For legacy format, get link_prekey_id and link_private_key
    if 'invite_blob' in link_data:
        # Unified format
        invite_prekey_id = link_data.get('invite_prekey_id')
        invite_private_key = crypto.b64decode(link_data['invite_private_key'])
        link_prekey_id = invite_prekey_id
        link_private_key = invite_private_key

        # Get user_id and network_id from the stored invite event
        unsafedb = create_unsafe_db(db)
        invite_blob = store.get(link_invite_id, unsafedb)
        invite_event = crypto.parse_json(invite_blob)
        user_id = invite_event.get('user_id')  # For mode='link'
        network_id = invite_event.get('network_id')
    else:
        # Legacy format
        link_prekey_id = link_data['link_prekey_id']
        link_private_key = crypto.b64decode(link_data['link_private_key'])
        user_id = link_data['user_id']
        network_id = link_data['network_id']

    log.info(f"link.join() extracted link_prekey_id={link_prekey_id[:20]}..., user_id={user_id[:20]}...")

    # 2. Create peer_shared signed by the link invite (proving access to the invite)
    from events.identity import peer_shared
    peer_shared_id = peer_shared.create(
        peer_id=peer_id,
        t_ms=t_ms,
        db=db,
        invite_id=link_invite_id,
        invite_private_key=link_private_key
    )
    log.info(f"link.join() created peer_shared: {peer_shared_id[:20]}...")

    # Create invite_accepted event FIRST to capture invite link data for event-sourcing
    # Use the SAME invite_accepted as user.join() - this ensures the same code path
    # and the invite_private_key is stored in group_prekeys for unsealing group_key_shared
    from events.identity import invite_accepted
    invite_accepted_id = invite_accepted.create(
        invite_id=link_invite_id,
        invite_prekey_id=link_prekey_id,
        invite_private_key=link_private_key,
        peer_id=peer_id,
        t_ms=t_ms + 1,
        db=db
    )
    log.info(f"link.join() created invite_accepted_id={invite_accepted_id[:20]}...")

    # Create link event with proof (also creates transit prekey)
    link_id, transit_prekey_shared_id, prekey_id = create(
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        link_invite_id=link_invite_id,
        link_private_key=link_private_key,
        user_id=user_id,
        t_ms=t_ms + 2,
        db=db
    )
    log.info(f"link.join() created link_id={link_id[:20]}...")

    # Project the link event immediately for the laptop's own view
    # This creates the user record in the users table that message.create() needs
    # to look up author_id when creating messages
    from events.network import recorded
    link_recorded_id = recorded.create(link_id, peer_id, t_ms + 3, db, return_dupes=True)
    recorded.project_ids([link_recorded_id], db)
    log.info(f"link.join() projected link event for own view: {link_id[:20]}...")

    # Phase 5: invite_proof removed - proof IS the signature on peer_shared event (signed_by=invite_id)

    return {
        'peer_id': peer_id,
        'peer_shared_id': peer_shared_id,
        'user_id': user_id,
        'network_id': network_id,
        'link_invite_id': link_invite_id,
        'invite_accepted_id': invite_accepted_id,  # Now using unified invite_accepted
        'link_id': link_id,
        'prekey_id': prekey_id,
        'transit_prekey_shared_id': transit_prekey_shared_id,
    }
