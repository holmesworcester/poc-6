"""Key shared event type (shareable symmetric key sealed to recipient prekey)."""
from typing import Any
import logging
import crypto
import store
from events import peer, key, prekey

log = logging.getLogger(__name__)


def create(key_id: str, peer_id: str, peer_shared_id: str,
           recipient_peer_id: str, t_ms: int, db: Any) -> str:
    """Create a shareable key_shared event from a local symmetric key.

    The symmetric key is sealed to the recipient's prekey using asymmetric encryption.

    Args:
        key_id: Local key event ID (symmetric key to share)
        peer_id: Local peer ID (for signing and seeing)
        peer_shared_id: Public peer ID (for created_by)
        recipient_peer_id: Recipient's peer_id (to get their prekey for sealing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        key_shared_id: The stored key_shared event ID
    """
    log.info(f"key_shared.create() creating key_shared for key_id={key_id}, recipient={recipient_peer_id}, t_ms={t_ms}")

    # Get symmetric key from local key event
    key_blob = store.get(key_id, db)
    if not key_blob:
        raise ValueError(f"key not found: {key_id}")

    key_data = crypto.parse_json(key_blob)
    symmetric_key_b64 = key_data['key']

    # Get recipient's prekey for wrapping (asymmetric encryption)
    recipient_prekey = prekey.get_transit_prekey_for_peer(recipient_peer_id, peer_id, db)
    if not recipient_prekey:
        raise ValueError(f"No prekey found for recipient peer: {recipient_peer_id}")

    # Create the inner event (to be wrapped to recipient's prekey)
    inner_event_data = {
        'type': 'key_shared',
        'key_id': key_id,  # Reference to the key being shared
        'symmetric_key': symmetric_key_b64,  # The actual key material
        'recipient_peer_id': recipient_peer_id,
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign the inner event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_inner_event = crypto.sign_event(inner_event_data, private_key)

    # Wrap (asymmetric encrypt) to recipient's prekey
    canonical = crypto.canonicalize_json(signed_inner_event)
    wrapped_blob = crypto.wrap(canonical, recipient_prekey, db)

    # Store event with recorded wrapper and projection
    key_shared_id = store.event(wrapped_blob, peer_id, t_ms, db)

    log.info(f"key_shared.create() created key_shared_id={key_shared_id}")
    return key_shared_id


def project(key_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project key_shared event into keys table and shareable_events."""
    log.info(f"key_shared.project() key_shared_id={key_shared_id}, seen_by={recorded_by}")

    # Get blob from store (already unwrapped by recorded)
    blob = store.get(key_shared_id, db)
    if not blob:
        log.warning(f"key_shared.project() blob not found for key_shared_id={key_shared_id}")
        return None

    # Unwrap (decrypt) - recorded should have already done this, but we need the plaintext
    plaintext, missing_keys = crypto.unwrap(blob, recorded_by, db)
    if not plaintext:
        # If we can't decrypt, this might be a key_shared wrapped to someone else's prekey
        # (e.g., invite prekey where creator doesn't have the private key)
        # Extract hint and check if it's an invite prekey
        import crypto as crypto_module
        hint_bytes = crypto_module.key.extract_id(blob)
        hint_id = crypto.b64encode(hint_bytes)

        # Check if this is in pre_keys (invite prekeys are added there)
        invite_prekey = db.query_one("SELECT peer_id FROM pre_keys WHERE peer_id = ?", (hint_id,))
        if invite_prekey:
            # This is a key_shared to an invite prekey - mark as shareable even though we can't decrypt
            # We need to extract created_by and created_at from the wrapped data if possible
            # For now, mark with current peer as creator
            log.info(f"key_shared.project() can't decrypt (wrapped to invite prekey {hint_id}), marking as valid anyway")
            db.execute(
                "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
                (key_shared_id, recorded_by)
            )
            return key_shared_id

        log.warning(f"key_shared.project() failed to unwrap key_shared_id={key_shared_id}")
        return None

    # Parse JSON
    event_data = crypto.parse_json(plaintext)

    # Only project the key if we're the intended recipient
    # Creator sees the event but doesn't get the key added to their keys table
    if event_data.get('recipient_peer_id') != recorded_by:
        log.info(f"key_shared.project() not for this peer (recipient={event_data.get('recipient_peer_id')}, peer={recorded_by}), skipping key projection")
        # Still mark as valid, but don't add key to keys table (shareable marking handled by recorded.py)
        db.execute(
            "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
            (key_shared_id, recorded_by)
        )
        return key_shared_id

    # Verify signature - get public key from created_by peer_shared
    from events import peer_shared
    created_by = event_data['created_by']
    public_key = peer_shared.get_public_key(created_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"key_shared.project() signature verification failed for key_shared_id={key_shared_id}")
        return None

    # Insert the shared key into our local keys table
    # Use the original key_id from the creator
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

    log.info(f"key_shared.project() added key {original_key_id} to local keys table")

    # Insert into keys_shared table to track this event
    db.execute(
        """INSERT OR IGNORE INTO keys_shared
           (key_shared_id, original_key_id, created_by, created_at, recipient_peer_id, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            key_shared_id,
            original_key_id,
            event_data['created_by'],
            event_data['created_at'],
            event_data['recipient_peer_id'],
            recorded_by,
            recorded_at
        )
    )

    # Mark event as valid for this peer (shareable marking handled by recorded.py)
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (key_shared_id, recorded_by)
    )

    # Also mark the original key_id as valid since we now have it
    db.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (original_key_id, recorded_by)
    )

    return key_shared_id


def share_key_with_group_members(key_id: str, group_id: str, peer_id: str,
                                   peer_shared_id: str, t_ms: int, db: Any) -> list[str]:
    """Create key_shared events for all members of a group.

    Args:
        key_id: The symmetric key to share
        group_id: Group whose members should receive the key
        peer_id: Local peer ID (creator)
        peer_shared_id: Public peer ID (creator)
        t_ms: Base timestamp
        db: Database connection

    Returns:
        List of key_shared event IDs created
    """
    log.info(f"key_shared.share_key_with_group_members() key={key_id}, group={group_id}")

    # Get all members of the group (excluding self)
    members = db.query(
        """SELECT DISTINCT u.peer_id
           FROM group_members gm
           JOIN users u ON gm.user_id = u.user_id
           WHERE gm.group_id = ? AND u.peer_id != ?""",
        (group_id, peer_shared_id)
    )

    key_shared_ids = []
    for i, member in enumerate(members):
        recipient_peer_id = member['peer_id']

        try:
            key_shared_id = create(
                key_id=key_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_peer_id=recipient_peer_id,
                t_ms=t_ms + i + 1,  # Increment timestamp for each member
                db=db
            )
            key_shared_ids.append(key_shared_id)
            log.info(f"key_shared.share_key_with_group_members() created key_shared for {recipient_peer_id}")
        except Exception as e:
            log.warning(f"key_shared.share_key_with_group_members() failed for {recipient_peer_id}: {e}")

    return key_shared_ids
