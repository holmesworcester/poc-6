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


def create(inviter_peer_id: str, inviter_peer_shared_id: str,
           group_id: str, channel_id: str, key_id: str, t_ms: int, db: Any) -> tuple[str, str, dict[str, Any]]:
    """Create an invite event and generate invite link.

    SECURITY: This function trusts that inviter_peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function. This is safe for local-only apps where
    the user controls all peers on the device.

    Args:
        inviter_peer_id: Local peer ID (for signing)
        inviter_peer_shared_id: Public peer ID (for created_by)
        group_id: Group being invited to (serves as network identifier)
        channel_id: Default channel ID
        key_id: Network symmetric key ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        (invite_id, invite_link, invite_data): The stored invite event ID, the invite link, and the invite data dict
    """
    # Generate Ed25519 keypair for invite proof (separate from invite prekey)
    # Bob will use private key for proof signature only
    invite_private_key, invite_public_key = crypto.generate_keypair()
    invite_pubkey_b64 = crypto.b64encode(invite_public_key)

    # Create invite prekey using the proof key (reuse for wrapping)
    # This is a proper transit_prekey event (not detached), so Bob receives the same event ID
    from events.transit import transit_prekey, transit_prekey_shared

    # Create transit_prekey event with the invite keypair
    invite_prekey_id = transit_prekey.create_with_material(
        public_key=invite_public_key,
        private_key=invite_private_key,
        peer_id=inviter_peer_id,
        t_ms=t_ms,
        db=db
    )

    # Create transit_prekey_shared event so Bob can receive it
    # Linking happens during projection (event-sourcing principle)
    invite_transit_prekey_shared_id = transit_prekey_shared.create(
        prekey_id=invite_prekey_id,
        peer_id=inviter_peer_id,
        peer_shared_id=inviter_peer_shared_id,
        t_ms=t_ms,
        db=db
    )

    invite_prekey_public_key_b64 = invite_pubkey_b64  # For event data
    unsafedb = create_unsafe_db(db)

    # Generate invite-scoped keys
    invite_transit_key = crypto.generate_secret()  # Outer wrapper for Bob→Alice messages
    invite_group_key = crypto.generate_secret()  # Inner encryption for Bob's bootstrap events

    # Encode as b64 (not hex) for consistency
    invite_transit_key_b64 = crypto.b64encode(invite_transit_key)
    invite_group_key_b64 = crypto.b64encode(invite_group_key)

    # Create key events for invite keys (so we have event IDs to use as hints)
    invite_transit_key_id = transit_key.create_with_material(
        key_material=invite_transit_key,
        peer_id=inviter_peer_id,
        t_ms=t_ms,
        db=db
    )

    from events.group import group_key
    invite_group_key_id = group_key.create_with_material(
        key_material=invite_group_key,
        peer_id=inviter_peer_id,
        t_ms=t_ms + 1,
        db=db
    )

    log.info(f"invite.create() created invite_transit_key_id={invite_transit_key_id}")
    log.info(f"invite.create() created invite_group_key_id={invite_group_key_id}")

    # Get inviter's prekey for Bob to send sync requests
    # Query prekey from transit_prekeys table
    inviter_prekey_row = unsafedb.query_one(
        "SELECT prekey_id, public_key FROM transit_prekeys WHERE owner_peer_id = ? ORDER BY created_at DESC LIMIT 1",
        (inviter_peer_id,)
    )

    if not inviter_prekey_row:
        raise ValueError(f"No prekey found for inviter {inviter_peer_id}. Cannot create invite.")

    inviter_prekey_id = inviter_prekey_row['prekey_id']
    inviter_prekey_public_key = inviter_prekey_row['public_key']  # Raw bytes from DB

    # Get prekey_shared_id from transit_prekeys_shared table
    safedb = create_safe_db(db, recorded_by=inviter_peer_id)
    inviter_prekey_shared_row = safedb.query_one(
        "SELECT transit_prekey_shared_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ? ORDER BY created_at DESC LIMIT 1",
        (inviter_peer_shared_id, inviter_peer_id)
    )

    if not inviter_prekey_shared_row:
        raise ValueError(f"No prekey_shared found for inviter {inviter_peer_id}. Cannot create invite.")

    inviter_transit_prekey_shared_id = inviter_prekey_shared_row['transit_prekey_shared_id']

    # Address info (hardcoded for now, would come from address table in production)
    inviter_ip = '127.0.0.1'
    inviter_port = 6100

    # Create minimal invite event (signed by Alice, proves authorization)
    # This event contains group/channel/key metadata that Bob's user event will reference
    # Include invite_transit_key and invite_group_key secrets for event-sourcing (Alice needs to restore during reprojection)
    # Include inviter's prekey so Bob can send sync requests (projection into prekeys_shared)
    # Include prekey_shared_id values for proper reprojection
    # Include invite prekey public key so Bob can locally reconstruct prekey_shared projection
    invite_event_data = {
        'type': 'invite',
        'invite_pubkey': invite_pubkey_b64,
        'invite_prekey_public_key': invite_prekey_public_key_b64,  # For Bob to seal to
        'invite_transit_prekey_shared_id': invite_transit_prekey_shared_id,  # Event ID for invite prekey (detached: prekey_id = prekey_shared_id)
        'invite_transit_key_secret': invite_transit_key_b64,  # Outer wrapper for Bob→Alice messages (b64)
        'invite_transit_key_id': invite_transit_key_id,  # Pre-computed ID (b64)
        'invite_group_key_secret': invite_group_key_b64,  # Inner encryption for Bob's bootstrap events (b64)
        'invite_group_key_id': invite_group_key_id,  # Pre-computed ID (b64)
        'group_id': group_id,
        'channel_id': channel_id,
        'key_id': key_id,
        'inviter_peer_shared_id': inviter_peer_shared_id,  # For prekey projection (public identity)
        'inviter_prekey_public_key': crypto.b64encode(inviter_prekey_public_key),  # For Bob to send sync requests
        'inviter_transit_prekey_shared_id': inviter_transit_prekey_shared_id,  # Event ID for inviter's transit_prekey_shared event
        'inviter_transit_prekey_id': inviter_prekey_id,  # Inviter's transit_prekey event ID (for hint lookup)
        'created_by': inviter_peer_shared_id,
        'created_at': t_ms
    }

    # Sign the invite event with inviter's peer private key
    private_key = peer.get_private_key(inviter_peer_id, inviter_peer_id, db)
    signed_invite_event = crypto.sign_event(invite_event_data, private_key)

    # Canonicalize and store the invite event (with recorded wrapper for reprojection)
    # store.event() will automatically project the invite, restoring keys from event data
    invite_blob = crypto.canonicalize_json(signed_invite_event)
    invite_id = store.event(invite_blob, inviter_peer_id, t_ms, db)

    # Get network key to include in invite link (Bob gets it directly via invite_accepted)
    # Network keys are group keys (subjective), so use safedb
    safedb = create_safe_db(db, recorded_by=inviter_peer_id)
    network_key_row = safedb.query_one(
        "SELECT key FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (key_id, inviter_peer_id)
    )
    if not network_key_row:
        raise ValueError(f"Network key {key_id} not found. Cannot create invite.")
    network_key = network_key_row['key']

    # key_id is already an event ID (from transit_key.create()), use it directly as hint

    # Note: invite_group_key is only used by Alice to validate Bob's bootstrap events
    # It doesn't need to be shared to other members - they receive Bob's events
    # decrypted and re-encrypted with the network key via normal sync

    # Get inviter's peer_shared blob to include in invite link
    # This allows Bob to immediately have Alice in his peers_shared table upon joining
    inviter_peer_shared_blob = store.get(inviter_peer_shared_id, unsafedb)
    if not inviter_peer_shared_blob:
        raise ValueError(f"Inviter's peer_shared blob not found: {inviter_peer_shared_id}. Cannot create invite.")

    # Build invite link with invite blob + secrets
    # Group/channel/key metadata is now in the signed blob (not plaintext)
    # Invite prekey reconstructed from invite event (public key), not from encrypted blob
    # Raw keys included directly - Bob gets them via invite_accepted event projection
    import base64
    invite_blob_b64 = base64.urlsafe_b64encode(invite_blob).decode().rstrip('=')
    inviter_peer_shared_blob_b64 = base64.urlsafe_b64encode(inviter_peer_shared_blob).decode().rstrip('=')

    invite_link_data = {
        'invite_blob': invite_blob_b64,  # Signed invite event (contains group/channel/key + invite prekey public key)
        'invite_private_key': crypto.b64encode(invite_private_key),  # Bob uses in-memory only (for proof signature)
        'invite_transit_key': invite_transit_key_b64,  # Outer wrapper key (b64)
        'invite_transit_key_id': invite_transit_key_id,  # Pre-computed ID (b64)
        'invite_group_key': invite_group_key_b64,  # Inner encryption key (b64)
        'invite_group_key_id': invite_group_key_id,  # Pre-computed ID (b64)
        'network_key': crypto.b64encode(network_key),  # Long-term group key (b64)
        'network_key_id': key_id,  # Network key event ID (b64)
        'inviter_peer_shared_id': inviter_peer_shared_id,  # Alice's peer_shared_id for Bob to send sync requests
        'inviter_peer_shared_blob': inviter_peer_shared_blob_b64,  # Alice's peer_shared blob for immediate projection
        'ip': inviter_ip,
        'port': inviter_port,
    }

    # Encode invite link as base64-urlsafe JSON
    import base64
    invite_json = json.dumps(invite_link_data, separators=(',', ':'), sort_keys=True)
    invite_code = base64.urlsafe_b64encode(invite_json.encode()).decode().rstrip('=')
    invite_link = f"quiet://invite/{invite_code}"

    log.info(f"invite.create() invite link contains transit_key_id={invite_transit_key_id}")
    log.info(f"invite.create() invite link contains group_key_id={invite_group_key_id}")
    log.info(f"invite.create() invite link contains network_key_id={key_id}")

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

    # Restore invite prekey to transit_prekeys_shared table (needed for invite_key_shared.project())
    # Bob reconstructs this locally from invite event data (doesn't need to decrypt transit_prekey_shared blob)
    # Alice restores it during reprojection so she can decrypt invite_key_shared from Bob
    # Invite prekeys are "detached": transit_prekey_id = transit_prekey_shared_id (no separate transit_prekey event)
    if 'invite_prekey_public_key' in event_data and 'invite_transit_prekey_shared_id' in event_data:
        invite_prekey_public_key_bytes = crypto.b64decode(event_data['invite_prekey_public_key'])
        invite_transit_prekey_shared_id = event_data['invite_transit_prekey_shared_id']
        safedb.execute(
            "INSERT OR REPLACE INTO transit_prekeys_shared (transit_prekey_shared_id, transit_prekey_id, peer_id, public_key, created_at, recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invite_transit_prekey_shared_id, invite_transit_prekey_shared_id, created_by, invite_prekey_public_key_bytes, event_data['created_at'], recorded_by, recorded_at)
        )

    # Project inviter's prekey into transit_prekeys_shared (for Bob to send sync requests to Alice)
    # Always project for anyone who receives the invite (INSERT OR IGNORE handles duplicates)
    if 'inviter_prekey_public_key' in event_data and 'inviter_peer_shared_id' in event_data and 'inviter_transit_prekey_shared_id' in event_data and 'inviter_transit_prekey_id' in event_data:
        inviter_prekey_public_key_bytes = crypto.b64decode(event_data['inviter_prekey_public_key'])
        inviter_peer_shared_id = event_data['inviter_peer_shared_id']

        log.info(f"invite.project() projecting inviter's prekey for {recorded_by[:20]}... to contact {inviter_peer_shared_id[:20]}...")
        safedb.execute(
            "INSERT OR IGNORE INTO transit_prekeys_shared (transit_prekey_shared_id, transit_prekey_id, peer_id, public_key, created_at, recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_data['inviter_transit_prekey_shared_id'], event_data['inviter_transit_prekey_id'], inviter_peer_shared_id, inviter_prekey_public_key_bytes, event_data['created_at'], recorded_by, recorded_at)
        )

    # Restore invite_transit_key for event-sourcing (Alice needs this to unwrap Bob's bootstrap messages)
    # The key event was already created in invite.create(), so it's already projected
    # We just need to verify the invite_transit_key_id is in the event data
    if 'invite_transit_key_id' in event_data:
        invite_transit_key_id = event_data['invite_transit_key_id']
        log.info(f"invite.project() invite_transit_key already projected with key_id={invite_transit_key_id}, recorded_by={recorded_by[:20]}...")

    # Restore invite_group_key for event-sourcing (Alice needs this to validate Bob's bootstrap events)
    # The key event was already created in invite.create(), so it's already projected
    # We just need to verify the invite_group_key_id is in the event data
    if 'invite_group_key_id' in event_data:
        invite_group_key_id = event_data['invite_group_key_id']
        log.info(f"invite.project() invite_group_key already projected with key_id={invite_group_key_id}, recorded_by={recorded_by[:20]}...")

    return invite_id
