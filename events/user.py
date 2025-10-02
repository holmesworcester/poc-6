"""User event type (shareable, encrypted) - represents network membership."""
from typing import Any
import crypto
import store
from events import key, peer


def create(peer_id: str, peer_shared_id: str, group_id: str,
           name: str, key_id: str, t_ms: int, db: Any,
           invite_secret: str | None = None) -> str:
    """Create a user event representing network membership.

    Args:
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by and peer_id in event)
        group_id: Group being joined (serves as network identifier)
        name: Display name for the user
        key_id: Key to encrypt the user event with
        t_ms: Timestamp
        db: Database connection
        invite_secret: Optional invite secret (if joining via invite)

    Returns:
        user_id: The stored user event ID
    """
    # Create base user event
    event_data = {
        'type': 'user',
        'peer_id': peer_shared_id,  # References the public peer identity
        'group_id': group_id,
        'name': name,
        'created_by': peer_shared_id,
        'created_at': t_ms,
        'key_id': key_id
    }

    # If joining via invite, add invite proof fields
    if invite_secret:
        # Get public key for signature (from peer_shared)
        from events import peer_shared
        public_key_bytes = peer_shared.get_public_key(peer_shared_id, peer_id, db)
        public_key_hex = public_key_bytes.hex()

        # Derive invite pubkey and signature
        invite_pubkey = crypto.derive_invite_pubkey(invite_secret)
        invite_signature = crypto.derive_invite_signature(invite_secret, public_key_hex, group_id)

        event_data['invite_pubkey'] = invite_pubkey
        event_data['invite_signature'] = invite_signature

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get key_data for encryption
    key_data = key.get_key(key_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event with first_seen wrapper and projection
    user_id = store.event(blob, peer_id, t_ms, db)

    return user_id


def project(user_id: str, seen_by_peer_id: str, received_at: int, db: Any) -> str | None:
    """Project user event into users and group_members tables."""
    # Get blob from store
    blob = store.get(user_id, db)
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

    # Validate invite proof if present
    if 'invite_pubkey' in event_data or 'invite_signature' in event_data:
        # Both fields must be present if either is
        if not event_data.get('invite_pubkey') or not event_data.get('invite_signature'):
            return None

        # Verify invite exists by querying invites table
        invite_row = db.query_one(
            "SELECT group_id FROM invites WHERE invite_pubkey = ?",
            (event_data['invite_pubkey'],)
        )

        if not invite_row:
            return None  # Invite not found

        # Verify invite's group_id matches user event
        if invite_row['group_id'] != event_data['group_id']:
            return None  # Group mismatch

    # Insert into users table
    db.execute(
        """INSERT OR IGNORE INTO users
           (user_id, peer_id, name, joined_at, invite_pubkey)
           VALUES (?, ?, ?, ?, ?)""",
        (
            user_id,
            event_data['peer_id'],
            event_data['name'],
            event_data['created_at'],
            event_data.get('invite_pubkey', '')
        )
    )

    # Insert into group_members table
    db.execute(
        """INSERT OR IGNORE INTO group_members
           (group_id, user_id, added_by, added_at)
           VALUES (?, ?, ?, ?)""",
        (
            event_data['group_id'],
            user_id,
            event_data['created_by'],  # Self-added for invite joins
            event_data['created_at']
        )
    )

    # Insert into shareable_events
    db.execute(
        """INSERT OR IGNORE INTO shareable_events (event_id, peer_id, created_at)
           VALUES (?, ?, ?)""",
        (
            user_id,
            event_data['created_by'],
            event_data['created_at']
        )
    )

    # Mark as valid for this peer
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, seen_by_peer_id) VALUES (?, ?)",
        (user_id, seen_by_peer_id)
    )

    return user_id
