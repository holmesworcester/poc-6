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

    NOW USES UNIFIED INVITE: Delegates to invite.create(mode='link') for consistency.

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
    # Get user_id for this peer to pass to unified invite
    safedb = create_safe_db(db, recorded_by=peer_id)

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
        "SELECT user_id FROM users WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, peer_id)
    )
    if not user_row:
        raise ValueError(f"User not found for peer {peer_id}")

    user_id = user_row['user_id']

    log.info(f"link_invite.create() delegating to invite.create(mode='link') for user_id={user_id}")

    # Delegate to unified invite.create with mode='link'
    from events.identity import invite
    return invite.create(peer_id=peer_id, t_ms=t_ms, db=db, mode='link', user_id=user_id)


def project(link_invite_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project link_invite event into link_invites table.

    For unified invites (type='invite' with mode='link'), delegates to invite.project().
    For legacy link_invite events (type='link_invite'), processes here for backward compatibility.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(link_invite_id, unsafedb)
    if not blob:
        return None

    # Parse JSON (plaintext, no unwrap needed)
    event_data = crypto.parse_json(blob)

    # Check if this is a unified invite (type='invite' with mode='link')
    if event_data.get('type') == 'invite' and event_data.get('mode') == 'link':
        log.info(f"link_invite.project() delegating to invite.project() for unified invite")
        from events.identity import invite
        return invite.project(link_invite_id, recorded_by, recorded_at, db)

    # Otherwise handle legacy link_invite format

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
