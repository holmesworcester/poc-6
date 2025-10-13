"""Key shared event type (shareable symmetric key sealed to recipient prekey)."""
from typing import Any
import logging
import crypto
import store
from events.transit import transit_key, transit_prekey as prekey
from events.identity import peer
from db import create_safe_db, create_unsafe_db

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
    recipient_prekey = transit_prekey.get_transit_prekey_for_peer(recipient_peer_id, peer_id, db)
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


def create_for_invite(key_id: str, peer_id: str, peer_shared_id: str,
                      recipient_prekey_dict: dict[str, Any], t_ms: int, db: Any) -> str:
    """Create a shareable key_shared event sealed to an arbitrary prekey (for invites).

    Unlike create(), this takes a prekey dict directly instead of looking up via peer_id.
    Used when sharing network key via invite (sealed to invite prekey).

    Args:
        key_id: Local key event ID (symmetric key to share)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        recipient_prekey_dict: Prekey dict with {id, public_key, type} for sealing
        t_ms: Timestamp
        db: Database connection

    Returns:
        key_shared_id: The stored key_shared event ID
    """
    log.info(f"key_shared.create_for_invite() creating key_shared for key_id={key_id}, t_ms={t_ms}")

    # Get symmetric key from local key event
    key_blob = store.get(key_id, db)
    if not key_blob:
        raise ValueError(f"key not found: {key_id}")

    key_data = crypto.parse_json(key_blob)
    symmetric_key_b64 = key_data['key']

    # Create inner event (use prekey id as recipient marker)
    recipient_marker = crypto.b64encode(recipient_prekey_dict['id'])
    inner_event_data = {
        'type': 'key_shared',
        'key_id': key_id,
        'symmetric_key': symmetric_key_b64,
        'recipient_peer_id': recipient_marker,  # Invite prekey ID
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign and wrap
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_inner_event = crypto.sign_event(inner_event_data, private_key)
    canonical = crypto.canonicalize_json(signed_inner_event)
    wrapped_blob = crypto.wrap(canonical, recipient_prekey_dict, db)

    # Store with recorded wrapper
    key_shared_id = store.event(wrapped_blob, peer_id, t_ms, db)

    log.info(f"key_shared.create_for_invite() created key_shared_id={key_shared_id}")
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
        hint_bytes = crypto_module.transit_key.extract_id(blob)
        hint_id = crypto.b64encode(hint_bytes)

        # Check if this is in transit_prekeys_shared (invite prekeys are added there)
        safedb_check = create_safe_db(db, recorded_by=recorded_by)
        invite_prekey = safedb_check.query_one("SELECT 1 FROM transit_prekeys_shared WHERE transit_prekey_shared_id = ? AND recorded_by = ?", (hint_id, recorded_by))
        if invite_prekey:
            # This is a key_shared to an invite prekey - mark as shareable even though we can't decrypt
            # We need to extract created_by and created_at from the wrapped data if possible
            # For now, mark with current peer as creator
            log.info(f"key_shared.project() can't decrypt (wrapped to invite prekey {hint_id}), marking as valid anyway")
            safedb = create_safe_db(db, recorded_by=recorded_by)
            safedb.execute(
                "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
                (key_shared_id, recorded_by)
            )
            return key_shared_id

        log.warning(f"key_shared.project() failed to unwrap key_shared_id={key_shared_id}")
        return None

    # Parse JSON
    event_data = crypto.parse_json(plaintext)

    # If we successfully decrypted it, we should add the key to our group_keys table
    # This handles both:
    # 1. Regular case: recipient_peer_id matches our peer_id
    # 2. Invite case: recipient_peer_id is invite prekey ID, but we have invite private key
    # The ability to decrypt is the authorization, not the recipient_peer_id field

    # Verify signature - get public key from created_by peer_shared
    from events.identity import peer_shared
    created_by = event_data['created_by']
    public_key = peer_shared.get_public_key(created_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"key_shared.project() signature verification failed for key_shared_id={key_shared_id}")
        return None

    # Insert the shared key into group_keys table
    # Use the original key_id from the creator
    original_key_id = event_data['key_id']
    symmetric_key = crypto.b64decode(event_data['symmetric_key'])

    safedb = create_safe_db(db, recorded_by=recorded_by)
    safedb.execute(
        """INSERT OR IGNORE INTO group_keys (key_id, key, recorded_by, created_at)
           VALUES (?, ?, ?, ?)""",
        (
            original_key_id,
            symmetric_key,
            recorded_by,
            event_data['created_at']
        )
    )

    log.info(f"key_shared.project() added key {original_key_id} to group_keys table")

    # Insert into group_keys_shared table to track this event
    safedb.execute(
        """INSERT OR IGNORE INTO group_keys_shared
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
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (key_shared_id, recorded_by)
    )

    # Also mark the original key_id as valid since we now have it
    safedb.execute(
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
    safedb = create_safe_db(db, recorded_by=peer_id)
    members = safedb.query(
        """SELECT DISTINCT u.peer_id
           FROM group_members gm
           JOIN users u ON gm.user_id = u.user_id AND u.recorded_by = gm.recorded_by
           WHERE gm.group_id = ? AND u.peer_id != ? AND gm.recorded_by = ?""",
        (group_id, peer_shared_id, peer_id)
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
