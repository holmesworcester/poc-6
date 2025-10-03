"""Invite event type (shareable, encrypted)."""
from typing import Any
import secrets
import json
import crypto
import store
from events import key, peer


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
    # Generate Ed25519 keypair for invite proof AND prekey wrapping
    # Bob will use private key for: 1) proof signature, 2) unwrapping key_shared
    invite_private_key, invite_public_key = crypto.generate_keypair()
    invite_pubkey_b64 = crypto.b64encode(invite_public_key)

    # Create pseudo peer_id for invite prekey (hash of public key)
    # This allows key_shared to wrap to it like any other peer's prekey
    invite_prekey_id = crypto.b64encode(crypto.hash(invite_public_key, size=16))

    # Insert invite public key into pre_keys table so key_shared can find it immediately
    # (Also restored via invite.project() for reprojection)
    db.execute(
        "INSERT OR REPLACE INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
        (invite_prekey_id, invite_public_key, t_ms)
    )

    # Generate invite-scoped key_secret for initial event encryption
    invite_key_secret = crypto.generate_secret()
    invite_key_secret_hex = invite_key_secret.hex()

    # Compute invite_key_id
    invite_key_id_bytes = crypto.hash(invite_key_secret, size=16)
    invite_key_id = crypto.b64encode(invite_key_id_bytes)

    # Store invite key_secret in keys table for immediate use
    # (Also restored via invite.project() for reprojection)
    db.execute(
        "INSERT OR REPLACE INTO keys (key_id, key, created_at) VALUES (?, ?, ?)",
        (invite_key_id, invite_key_secret, t_ms)
    )

    # Track key ownership for routing (supports multiple local peers with same key)
    # (Also restored via invite.project() for reprojection)
    db.execute(
        "INSERT OR IGNORE INTO key_ownership (key_id, peer_id, created_at) VALUES (?, ?, ?)",
        (invite_key_id, inviter_peer_id, t_ms)
    )

    # Get inviter's prekey for Bob to send sync requests
    # Check if this peer has a prekey registered (query from local_prekeys)
    inviter_prekey_row = db.query_one(
        "SELECT prekey_id FROM local_prekeys WHERE owner_peer_id = ? LIMIT 1",
        (inviter_peer_id,)
    )
    # inviter_prekey_id is the peer_id (pre_keys table maps peer_id -> public_key)
    inviter_prekey_id = inviter_peer_id if inviter_prekey_row else None

    # Address info (hardcoded for now, would come from address table in production)
    inviter_ip = '127.0.0.1'
    inviter_port = 6100

    # Create minimal invite event (signed by Alice, proves authorization)
    # This event contains group/channel/key metadata that Bob's user event will reference
    # Include invite_key_secret for event-sourcing (Alice needs to restore it during reprojection)
    invite_event_data = {
        'type': 'invite',
        'invite_pubkey': invite_pubkey_b64,
        'invite_key_secret': invite_key_secret_hex,
        'group_id': group_id,
        'channel_id': channel_id,
        'key_id': key_id,
        'created_by': inviter_peer_shared_id,
        'created_at': t_ms
    }

    # Sign the invite event with inviter's peer private key
    private_key = peer.get_private_key(inviter_peer_id, inviter_peer_id, db)
    signed_invite_event = crypto.sign_event(invite_event_data, private_key)

    # Canonicalize and store the invite event (with recorded wrapper for reprojection)
    invite_blob = crypto.canonicalize_json(signed_invite_event)
    invite_id = store.event(invite_blob, inviter_peer_id, t_ms, db)

    # Create invite_key_shared event wrapping the group key to the invite prekey
    # This gives Bob his copy of the group key when he joins
    from events import invite_key_shared
    invite_key_shared_id = invite_key_shared.create(
        key_id=key_id,
        peer_id=inviter_peer_id,
        peer_shared_id=inviter_peer_shared_id,
        invite_prekey_id=invite_prekey_id,
        invite_public_key=invite_public_key,
        t_ms=t_ms + 1,
        db=db
    )

    # Build invite link with invite blob + secrets
    # Group/channel/key metadata is now in the signed blob (not plaintext)
    import base64
    invite_blob_b64 = base64.urlsafe_b64encode(invite_blob).decode().rstrip('=')

    invite_link_data = {
        'invite_blob': invite_blob_b64,  # Signed invite event (contains group/channel/key)
        'invite_private_key': crypto.b64encode(invite_private_key),
        'invite_public_key': crypto.b64encode(invite_public_key),
        'invite_prekey_id': invite_prekey_id,  # Bob needs this to store private key
        'invite_key_secret': invite_key_secret_hex,
        'invite_key_shared_id': invite_key_shared_id,  # Reference to the invite_key_shared event
        'inviter_prekey_id': inviter_prekey_id,  # Alice's prekey for Bob to send sync requests
        'ip': inviter_ip,
        'port': inviter_port,
    }

    # Encode invite link as base64-urlsafe JSON
    import base64
    invite_json = json.dumps(invite_link_data, separators=(',', ':'), sort_keys=True)
    invite_code = base64.urlsafe_b64encode(invite_json.encode()).decode().rstrip('=')
    invite_link = f"quiet://invite/{invite_code}"

    return (invite_id, invite_link, invite_link_data)


def project(invite_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project minimal invite event into invites table."""
    # Get blob from store (now just canonicalized JSON, not encrypted)
    blob = store.get(invite_id, db)
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
    db.execute(
        """INSERT OR IGNORE INTO invites
           (invite_id, invite_pubkey, group_id, inviter_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            invite_id,
            event_data['invite_pubkey'],
            event_data['group_id'],
            created_by,  # Use created_by (inviter's peer_shared_id)
            event_data['created_at']
        )
    )

    # Restore invite prekey to pre_keys table (needed for invite_key_shared.project())
    # The invite prekey is a pseudo-prekey used for wrapping the invite_key_shared event
    invite_pubkey_bytes = crypto.b64decode(event_data['invite_pubkey'])
    invite_prekey_id = crypto.b64encode(crypto.hash(invite_pubkey_bytes, size=16))
    db.execute(
        "INSERT OR REPLACE INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
        (invite_prekey_id, invite_pubkey_bytes, event_data['created_at'])
    )

    # Restore invite_key_secret for event-sourcing (Alice needs this to unwrap Bob's bootstrap events)
    if 'invite_key_secret' in event_data:
        invite_key_secret = bytes.fromhex(event_data['invite_key_secret'])
        invite_key_id_bytes = crypto.hash(invite_key_secret, size=16)
        invite_key_id = crypto.b64encode(invite_key_id_bytes)

        db.execute(
            "INSERT OR REPLACE INTO keys (key_id, key, created_at) VALUES (?, ?, ?)",
            (invite_key_id, invite_key_secret, event_data['created_at'])
        )

        # Track key ownership for routing (supports multiple local peers with same key)
        # INSERT OR IGNORE is safe for reprojection - idempotent
        db.execute(
            "INSERT OR IGNORE INTO key_ownership (key_id, peer_id, created_at) VALUES (?, ?, ?)",
            (invite_key_id, recorded_by, event_data['created_at'])
        )

    return invite_id
