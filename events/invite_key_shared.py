"""Invite key shared event type (group key wrapped to invite prekey for bootstrap)."""
from typing import Any
import logging
import crypto
import store
from events import peer, key

log = logging.getLogger(__name__)


def create(key_id: str, peer_id: str, peer_shared_id: str,
           invite_prekey_id: str, invite_public_key: bytes, t_ms: int, db: Any) -> str:
    """Create an invite_key_shared event wrapping a group key to an invite prekey.

    This is used during invite creation to share the group key with the future invitee.
    The creator cannot decrypt this event (they don't have the invite private key).

    Args:
        key_id: Local key event ID (group key to share)
        peer_id: Local peer ID (inviter, for signing)
        peer_shared_id: Public peer ID (inviter, for created_by)
        invite_prekey_id: ID of the invite prekey (recipient identifier)
        invite_public_key: Invite's public key (for wrapping)
        t_ms: Timestamp
        db: Database connection

    Returns:
        invite_key_shared_id: The stored event ID
    """
    log.info(f"invite_key_shared.create() creating for key_id={key_id}, invite_prekey={invite_prekey_id}")

    # Get symmetric key from local key event
    key_blob = store.get(key_id, db)
    if not key_blob:
        raise ValueError(f"key not found: {key_id}")

    key_data = crypto.parse_json(key_blob)
    symmetric_key_b64 = key_data['key']

    # Create the inner event (to be wrapped to invite's prekey)
    inner_event_data = {
        'type': 'invite_key_shared',
        'key_id': key_id,  # Reference to the key being shared
        'symmetric_key': symmetric_key_b64,  # The actual key material
        'recipient_invite_prekey_id': invite_prekey_id,
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign the inner event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_inner_event = crypto.sign_event(inner_event_data, private_key)

    # Wrap (asymmetric encrypt) to invite's public key
    canonical = crypto.canonicalize_json(signed_inner_event)

    # Build prekey dict for crypto.wrap()
    invite_prekey_dict = {
        'id': crypto.b64decode(invite_prekey_id),  # ID hint for unwrapping
        'type': 'asymmetric',
        'public_key': invite_public_key
    }
    wrapped_blob = crypto.wrap(canonical, invite_prekey_dict, db)

    # Store event with recorded wrapper and projection
    invite_key_shared_id = store.event(wrapped_blob, peer_id, t_ms, db)

    log.info(f"invite_key_shared.create() created invite_key_shared_id={invite_key_shared_id}")
    return invite_key_shared_id


def project(invite_key_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project invite_key_shared event.

    Creator (inviter): Cannot decrypt, but marks as shareable/valid anyway.
    Recipient (invitee with private key): Decrypts and adds key to local keys table.
    """
    log.info(f"invite_key_shared.project() id={invite_key_shared_id}, seen_by={recorded_by}")

    # Get blob from store
    blob = store.get(invite_key_shared_id, db)
    if not blob:
        log.warning(f"invite_key_shared.project() blob not found for id={invite_key_shared_id}")
        return None

    # Try to unwrap
    plaintext, missing_keys = crypto.unwrap(blob, recorded_by, db)

    if not plaintext:
        # Creator doesn't have the invite private key - this is expected
        # Check if this is wrapped to an invite prekey (in pre_keys table)
        hint_bytes = key.extract_id(blob)
        hint_id = crypto.b64encode(hint_bytes)

        invite_prekey = db.query_one("SELECT peer_id FROM pre_keys WHERE peer_id = ?", (hint_id,))
        if invite_prekey:
            # This is expected - creator can't decrypt invite_key_shared events
            # Mark as shareable and valid anyway so recipient can receive it
            log.info(f"invite_key_shared.project() creator can't decrypt (expected), marking shareable")
            # Will be marked shareable by centralized logic in recorded.py
            # Just mark as valid here
            db.execute(
                "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
                (invite_key_shared_id, recorded_by)
            )
            return invite_key_shared_id

        log.warning(f"invite_key_shared.project() failed to unwrap id={invite_key_shared_id}")
        return None

    # Successfully decrypted - recipient has the invite private key
    event_data = crypto.parse_json(plaintext)

    # Verify signature
    from events import peer_shared
    created_by = event_data['created_by']
    public_key = peer_shared.get_public_key(created_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"invite_key_shared.project() signature verification failed for id={invite_key_shared_id}")
        return None

    # Add the shared key to our local keys table
    original_key_id = event_data['key_id']
    symmetric_key = crypto.b64decode(event_data['symmetric_key'])

    db.execute(
        """INSERT OR IGNORE INTO keys (key_id, key, created_at)
           VALUES (?, ?, ?)""",
        (
            original_key_id,
            symmetric_key,
            event_data['created_at']
        )
    )

    log.info(f"invite_key_shared.project() added key {original_key_id} to local keys table")

    # Track this in invite_keys_shared table
    log.info(f">>> INSERTING into invite_keys_shared: event={invite_key_shared_id[:20]}..., seen_by={recorded_by[:20]}..., recipient={event_data['recipient_invite_prekey_id'][:20]}...")
    db.execute(
        """INSERT OR IGNORE INTO invite_keys_shared
           (invite_key_shared_id, original_key_id, created_by, created_at,
            recipient_invite_prekey_id, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            invite_key_shared_id,
            original_key_id,
            event_data['created_by'],
            event_data['created_at'],
            event_data['recipient_invite_prekey_id'],
            recorded_by,
            recorded_at
        )
    )

    # Log if insert actually happened
    if db.changes() > 0:
        log.info(f">>> Row INSERTED (new row)")
    else:
        log.info(f">>> Row IGNORED (duplicate)")

    # Mark the original key_id as valid since we now have it
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (original_key_id, recorded_by)
    )

    return invite_key_shared_id
