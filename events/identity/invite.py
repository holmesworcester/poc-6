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


def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, str, dict[str, Any]]:
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

    Returns:
        (invite_id, invite_link, invite_data): The stored invite event ID, the invite link, and the invite data dict
    """
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

    # Query for main group and main channel

    # Get main group
    group_row = safedb.query_one(
        "SELECT group_id, key_id FROM groups WHERE recorded_by = ? AND is_main = 1 LIMIT 1",
        (peer_id,)
    )
    if not group_row:
        raise ValueError(f"No main group found for peer {peer_id}. Cannot create invite.")

    group_id = group_row['group_id']
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
        "SELECT prekey_id, public_key FROM transit_prekeys WHERE owner_peer_id = ? ORDER BY created_at DESC LIMIT 1",
        (peer_id,)
    )

    if not inviter_prekey_row:
        raise ValueError(f"No prekey found for inviter {peer_id}. Cannot create invite.")

    inviter_prekey_id = inviter_prekey_row['prekey_id']
    inviter_prekey_public_key = inviter_prekey_row['public_key']  # Raw bytes from DB

    # Get prekey_shared_id from transit_prekeys_shared table
    inviter_prekey_shared_row = safedb.query_one(
        "SELECT transit_prekey_shared_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ? ORDER BY created_at DESC LIMIT 1",
        (peer_shared_id, peer_id)
    )

    if not inviter_prekey_shared_row:
        raise ValueError(f"No prekey_shared found for inviter {peer_id}. Cannot create invite.")

    inviter_transit_prekey_shared_id = inviter_prekey_shared_row['transit_prekey_shared_id']

    # Address info (hardcoded for now, would come from address table in production)
    inviter_ip = '127.0.0.1'
    inviter_port = 6100

    # Create minimal invite event (signed by Alice, proves authorization)
    # This event contains group/channel/key metadata that Bob's user event will reference
    # Include inviter's prekey so Bob can send sync requests (projection into prekeys_shared)
    invite_event_data = {
        'type': 'invite',
        'invite_pubkey': invite_pubkey_b64,  # For user proof signature
        'invite_prekey_id': invite_prekey_id,  # Crypto hint for GKS (deterministic hash)
        'group_id': group_id,
        'channel_id': channel_id,
        'key_id': key_id,
        'inviter_peer_shared_id': peer_shared_id,
        'inviter_transit_prekey_public_key': crypto.b64encode(inviter_prekey_public_key),
        'inviter_transit_prekey_shared_id': inviter_transit_prekey_shared_id,
        'inviter_transit_prekey_id': inviter_prekey_id,
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

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

    group_key_shared_id = group_key_shared.create_for_invite(
        key_id=key_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        invite_id=invite_id,  # Pass invite_id to extract prekey from stored invite
        t_ms=t_ms + 3,
        db=db
    )

    log.info(f"invite.create() created group_key_shared {group_key_shared_id[:20]}... for network key")

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

    # Note: Signature verification skipped during projection
    # For inviter: they created it locally (already verified)
    # For invitee: they trust the URL itself as the credential
    # The signature exists in the blob for future verification if needed

    created_by = event_data['created_by']

    # Insert into invites table
    safedb.execute(
        """INSERT OR IGNORE INTO invites
           (invite_id, invite_pubkey, group_id, inviter_id, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            invite_id,
            event_data['invite_pubkey'],
            event_data['group_id'],
            created_by,  # Use created_by (inviter's peer_shared_id)
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )

    # Project inviter's prekey into transit_prekeys_shared (for Bob to send sync requests to Alice)
    # Always project for anyone who receives the invite (INSERT OR IGNORE handles duplicates)
    if 'inviter_transit_prekey_public_key' in event_data and 'inviter_peer_shared_id' in event_data and 'inviter_transit_prekey_shared_id' in event_data and 'inviter_transit_prekey_id' in event_data:
        inviter_prekey_public_key_bytes = crypto.b64decode(event_data['inviter_transit_prekey_public_key'])
        inviter_peer_shared_id = event_data['inviter_peer_shared_id']

        log.info(f"invite.project() projecting inviter's prekey for {recorded_by[:20]}... to contact {inviter_peer_shared_id[:20]}...")
        safedb.execute(
            "INSERT OR IGNORE INTO transit_prekeys_shared (transit_prekey_shared_id, transit_prekey_id, peer_id, public_key, created_at, recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_data['inviter_transit_prekey_shared_id'], event_data['inviter_transit_prekey_id'], inviter_peer_shared_id, inviter_prekey_public_key_bytes, event_data['created_at'], recorded_by, recorded_at)
        )

    # Mark invite as valid for this peer (required for invite_accepted dependencies)
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_id, recorded_by)
    )

    return invite_id
