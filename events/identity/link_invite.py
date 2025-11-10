"""Link invite event type (shareable) - allows linking devices to same user."""
from typing import Any
import json
import logging
import crypto
import store
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, str, dict[str, Any]]:
    """Create a link invite event and generate link URL for multi-device linking.

    Automatically queries for the linking user's user_id, network_id, and groups.

    SECURITY: This function trusts that peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function.

    Args:
        peer_id: Local peer ID of the existing device
        t_ms: Timestamp
        db: Database connection

    Returns:
        (link_invite_id, link_url, link_data): The stored link invite event ID, the link URL, and link data dict
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    unsafedb = create_unsafe_db(db)

    # Query peer_self to get peer_shared_id
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set")

    peer_shared_id = peer_self_row['peer_shared_id']

    # Get user_id for this peer
    user_row = safedb.query_one(
        "SELECT user_id, network_id FROM users WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, peer_id)
    )
    if not user_row:
        raise ValueError(f"User not found for peer {peer_id}")

    user_id = user_row['user_id']
    network_id = user_row['network_id']

    log.info(f"link_invite.create() for user_id={user_id}, peer_id={peer_id}")

    # Generate Ed25519 keypair for link proof + GKS decryption
    # Same pattern as invite
    link_private_key, link_public_key = crypto.generate_keypair()
    link_pubkey_b64 = crypto.b64encode(link_public_key)

    # Generate deterministic prekey ID from public key hash
    link_prekey_id = crypto.b64encode(crypto.hash(link_public_key)[:16])

    # Get existing device's transit prekey for new device to send sync requests
    existing_prekey_row = unsafedb.query_one(
        "SELECT transit_prekey_id, public_key FROM transit_prekeys WHERE owner_peer_id = ? ORDER BY created_at DESC LIMIT 1",
        (peer_id,)
    )

    if not existing_prekey_row:
        raise ValueError(f"No prekey found for peer {peer_id}")

    existing_prekey_id = existing_prekey_row['transit_prekey_id']
    existing_prekey_public_key = existing_prekey_row['public_key']

    # Get prekey_shared info
    existing_prekey_shared_row = safedb.query_one(
        "SELECT transit_prekey_shared_id, created_at FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ? ORDER BY created_at DESC LIMIT 1",
        (peer_shared_id, peer_id)
    )

    if not existing_prekey_shared_row:
        raise ValueError(f"No prekey_shared found for peer {peer_id}")

    existing_prekey_shared_id = existing_prekey_shared_row['transit_prekey_shared_id']
    existing_prekey_shared_created_at = existing_prekey_shared_row['created_at']

    # Address info (hardcoded for now)
    existing_ip = '127.0.0.1'
    existing_port = 6100

    # Create link_invite event (signed by existing peer)
    # Similar structure to invite event but for device linking
    link_invite_event_data = {
        'type': 'link_invite',
        'link_pubkey': link_pubkey_b64,  # For link proof signature
        'link_prekey_id': link_prekey_id,  # Crypto hint for GKS (deterministic hash)
        'user_id': user_id,  # User being linked to
        'network_id': network_id,
        'existing_peer_shared_id': peer_shared_id,  # Existing device's peer
        'existing_transit_prekey_public_key': crypto.b64encode(existing_prekey_public_key),
        'existing_transit_prekey_shared_id': existing_prekey_shared_id,
        'existing_transit_prekey_shared_created_at': existing_prekey_shared_created_at,
        'existing_transit_prekey_id': existing_prekey_id,
        'expiry_ms': t_ms + 86400000,  # 24 hour expiry
        'max_joiners': 1,  # Only one device can join via this link
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign the link_invite event with peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_link_invite_event = crypto.sign_event(link_invite_event_data, private_key)

    # Store the link_invite event
    link_invite_blob = crypto.canonicalize_json(signed_link_invite_event)
    link_invite_id = store.event(link_invite_blob, peer_id, t_ms, db)

    log.info(f"link_invite.create() stored link_invite_id={link_invite_id[:20]}...")

    # Create group_key_shared events for all groups the user is in
    # So the new device can immediately decrypt group keys
    from events.group import group_key_shared

    group_rows = safedb.query(
        "SELECT DISTINCT g.group_id, g.key_id FROM groups g WHERE g.recorded_by = ? ORDER BY g.group_id",
        (peer_id,)
    )

    ts = t_ms + 1
    for group_row in group_rows:
        group_id = group_row['group_id']
        key_id = group_row['key_id']

        # Create group_key_shared sealed to link_prekey
        # This follows the same pattern as create_for_invite
        group_key_shared_id = group_key_shared.create_for_link_invite(
            key_id=key_id,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            link_invite_id=link_invite_id,
            t_ms=ts,
            db=db
        )
        ts += 1

        log.info(f"link_invite.create() created group_key_shared {group_key_shared_id[:20]}... for group {group_id[:20]}...")

    # Get existing device's peer_shared and user blobs for new device to immediately project
    existing_peer_shared_blob = store.get(peer_shared_id, unsafedb)
    if not existing_peer_shared_blob:
        raise ValueError(f"Existing device's peer_shared blob not found: {peer_shared_id}")

    # Also get user event blob so new device can accept events from existing peer immediately
    existing_user_blob = store.get(user_id, unsafedb)
    if not existing_user_blob:
        raise ValueError(f"User event blob not found: {user_id}")

    # Build link URL with link_invite blob + secrets
    import base64
    link_invite_blob_b64 = base64.urlsafe_b64encode(link_invite_blob).decode().rstrip('=')
    existing_peer_shared_blob_b64 = base64.urlsafe_b64encode(existing_peer_shared_blob).decode().rstrip('=')
    existing_user_blob_b64 = base64.urlsafe_b64encode(existing_user_blob).decode().rstrip('=')

    link_data = {
        'link_invite_blob': link_invite_blob_b64,  # Signed link_invite event
        'link_invite_id': link_invite_id,
        'link_prekey_id': link_prekey_id,  # Crypto hint
        'link_private_key': crypto.b64encode(link_private_key),  # Key material for GKS + proof
        'user_id': user_id,  # User being linked to
        'network_id': network_id,
        'existing_peer_shared_id': peer_shared_id,  # Existing device's peer
        'existing_peer_shared_blob': existing_peer_shared_blob_b64,  # For immediate projection
        'existing_user_blob': existing_user_blob_b64,  # User event for dependency resolution
        'existing_ip': existing_ip,
        'existing_port': existing_port,
    }

    # Encode link URL
    link_json = json.dumps(link_data, separators=(',', ':'), sort_keys=True)
    link_code = base64.urlsafe_b64encode(link_json.encode()).decode().rstrip('=')
    link_url = f"quiet://link/{link_code}"

    log.info(f"link_invite.create() link URL created with link_prekey_id={link_prekey_id[:20]}...")

    return (link_invite_id, link_url, link_data)


def project(link_invite_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project link_invite event into link_invites table."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(link_invite_id, unsafedb)
    if not blob:
        return None

    # Parse JSON (plaintext, no unwrap needed)
    event_data = crypto.parse_json(blob)

    created_by = event_data['created_by']

    # Check if bootstrap (peer has no networks yet - first bootstrap)
    peer_networks = safedb.query_one(
        "SELECT 1 FROM networks WHERE recorded_by = ? LIMIT 1",
        (recorded_by,)
    )

    is_bootstrap = (peer_networks is None)

    # Validate link_invite if not bootstrap
    if not is_bootstrap:
        log.info(f"link_invite.project() sync-sourced link_invite, validating...")

        # Verify creator exists
        from events.identity import peer_shared
        creator_public_key = peer_shared.get_public_key(created_by, recorded_by, db)
        if not creator_public_key:
            log.warning(f"link_invite.project() creator not found: {created_by[:20]}...")
            return None

        # Verify signature
        if not crypto.verify_event(event_data, creator_public_key):
            log.warning(f"link_invite.project() signature verification FAILED")
            return None

        # Verify not expired
        expiry_ms = event_data.get('expiry_ms', recorded_at + 86400000)
        if recorded_at > expiry_ms:
            log.warning(f"link_invite.project() EXPIRED: recorded_at={recorded_at} > expiry_ms={expiry_ms}")
            return None

        log.info(f"link_invite.project() validation passed")
    else:
        log.info(f"link_invite.project() bootstrap link_invite, skipping validation")

    # Insert into link_invites table
    safedb.execute(
        """INSERT OR IGNORE INTO link_invites
           (link_invite_id, link_pubkey, user_id, network_id, expiry_ms, max_joiners, created_at, recorded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            link_invite_id,
            event_data['link_pubkey'],
            event_data['user_id'],
            event_data.get('network_id'),
            event_data.get('expiry_ms'),
            event_data.get('max_joiners', 1),
            event_data['created_at'],
            recorded_by
        )
    )

    # Project existing device's prekey into transit_prekeys_shared
    if 'existing_transit_prekey_public_key' in event_data and 'existing_peer_shared_id' in event_data:
        existing_prekey_public_key_bytes = crypto.b64decode(event_data['existing_transit_prekey_public_key'])
        existing_peer_shared_id = event_data['existing_peer_shared_id']
        prekey_created_at = event_data.get('existing_transit_prekey_shared_created_at', event_data['created_at'])

        log.info(f"link_invite.project() projecting existing device's prekey")
        safedb.execute(
            "INSERT OR IGNORE INTO transit_prekeys_shared (transit_prekey_shared_id, transit_prekey_id, peer_id, public_key, created_at, recorded_by) VALUES (?, ?, ?, ?, ?, ?)",
            (event_data['existing_transit_prekey_shared_id'], event_data['existing_transit_prekey_id'], existing_peer_shared_id, existing_prekey_public_key_bytes, prekey_created_at, recorded_by)
        )

    # Mark link_invite as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (link_invite_id, recorded_by)
    )

    return link_invite_id
