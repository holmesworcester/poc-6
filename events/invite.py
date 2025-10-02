"""Invite event type (shareable, encrypted)."""
from typing import Any
import secrets
import json
import crypto
import store
from events import key, peer


def create(inviter_peer_id: str, inviter_peer_shared_id: str,
           group_id: str, key_id: str, t_ms: int, db: Any) -> tuple[str, str, dict[str, Any]]:
    """Create an invite event and generate invite link.

    Args:
        inviter_peer_id: Local peer ID (for signing)
        inviter_peer_shared_id: Public peer ID (for created_by)
        group_id: Group being invited to (serves as network identifier)
        key_id: Key to encrypt the invite event with
        t_ms: Timestamp
        db: Database connection

    Returns:
        (invite_id, invite_link, invite_data): The stored invite event ID, the invite link, and the invite data dict
    """
    # Generate Ed25519 keypair for invite proof
    # Bob will sign with private key, Alice stores public key in invite event
    invite_private_key, invite_public_key = crypto.generate_keypair()
    invite_pubkey_b64 = crypto.b64encode(invite_public_key)

    # Generate invite-scoped key_secret for initial event encryption
    invite_key_secret = crypto.generate_secret()
    invite_key_secret_hex = invite_key_secret.hex()

    # Store invite key_secret locally (create as owner's key)
    invite_key_id = key.create(inviter_peer_id, t_ms, db)

    # Generate transit secret for DEM transport bootstrap
    transit_secret = crypto.generate_secret()
    transit_secret_id = crypto.hash(transit_secret, size=32).hex()

    # Store transit secret locally (create as owner's key)
    transit_key_id = key.create(inviter_peer_id, t_ms, db)

    # TODO: Get inviter's address and prekey (hardcoded for now)
    # In production, query address and prekey tables
    inviter_ip = '127.0.0.1'
    inviter_port = 6100
    inviter_prekey_public = None  # TODO: Add when prekey system is ready
    inviter_prekey_secret_id = None

    # Create invite event with all join metadata
    event_data = {
        'type': 'invite',
        'invite_pubkey': invite_pubkey_b64,
        'group_id': group_id,
        'inviter_id': inviter_peer_shared_id,
        'invite_key_secret_id': invite_key_id,
        'transit_secret_id': transit_secret_id,
        'ip': inviter_ip,
        'port': inviter_port,
        'created_by': inviter_peer_shared_id,
        'created_at': t_ms,
        'key_id': key_id
    }

    # Add prekey info if available
    if inviter_prekey_public:
        event_data['prekey_public'] = inviter_prekey_public
    if inviter_prekey_secret_id:
        event_data['prekey_secret_id'] = inviter_prekey_secret_id

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(inviter_peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get key_data for encryption
    key_data = key.get_key(key_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event with first_seen wrapper and projection
    invite_id = store.event(blob, inviter_peer_id, t_ms, db)

    # Get the invite blob back for inclusion in the link
    invite_blob_bytes = store.get(invite_id, db)

    # Build invite link with minimal data: just blob + secrets
    # All metadata (group_id, ip, port, prekey) is in the blob
    # TODO: Future optimization - minify JSON before encryption
    # - Shorten field names (invite_pubkey → ipk)
    # - Remove redundant fields (created_by == inviter_id)
    # - Compress with zlib before base64
    # Target: reduce link from ~1055 chars to ~520 chars
    invite_link_data = {
        'invite_blob': crypto.b64encode(invite_blob_bytes),
        'invite_private_key': crypto.b64encode(invite_private_key),
        'invite_public_key': crypto.b64encode(invite_public_key),
        'invite_key_secret': invite_key_secret_hex,
        'transit_secret': transit_secret.hex(),
        'transit_secret_id': transit_secret_id,
        'ip': inviter_ip,
        'port': inviter_port
    }

    # Encode invite link as base64-urlsafe JSON
    import base64
    invite_json = json.dumps(invite_link_data, separators=(',', ':'), sort_keys=True)
    invite_code = base64.urlsafe_b64encode(invite_json.encode()).decode().rstrip('=')
    invite_link = f"quiet://invite/{invite_code}"

    return (invite_id, invite_link, invite_link_data)


def project(invite_id: str, seen_by_peer_id: str, received_at: int, db: Any) -> str | None:
    """Project invite event into invites table."""
    # Get blob from store
    blob = store.get(invite_id, db)
    if not blob:
        return None

    # Unwrap (decrypt)
    unwrapped, _ = crypto.unwrap(blob, db)
    if not unwrapped:
        return None

    # Parse JSON
    event_data = crypto.parse_json(unwrapped)

    # Verify signature - get public key from created_by peer_shared
    from events import peer_shared
    created_by = event_data['created_by']
    public_key = peer_shared.get_public_key(created_by, seen_by_peer_id, db)
    if not crypto.verify_event(event_data, public_key):
        return None

    # Insert into invites table
    db.execute(
        """INSERT OR IGNORE INTO invites
           (invite_id, invite_pubkey, group_id, inviter_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            invite_id,
            event_data['invite_pubkey'],
            event_data['group_id'],
            event_data['inviter_id'],
            event_data['created_at']
        )
    )

    # Insert into shareable_events
    db.execute(
        """INSERT OR IGNORE INTO shareable_events (event_id, peer_id, created_at)
           VALUES (?, ?, ?)""",
        (
            invite_id,
            event_data['created_by'],
            event_data['created_at']
        )
    )

    # Mark as valid for this peer (user events depend on invite events)
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, seen_by_peer_id) VALUES (?, ?)",
        (invite_id, seen_by_peer_id)
    )

    return invite_id
