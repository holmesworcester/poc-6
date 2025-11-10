"""Key shared event type (shareable symmetric key sealed to recipient prekey)."""
from typing import Any
import logging
import crypto
import store
from events.transit import transit_key, transit_prekey
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
    # Note: recipient identity is in the crypto hint (from wrap()), not in event data
    inner_event_data = {
        'type': 'group_key_shared',
        'key_id': key_id,  # Reference to the key being shared
        'symmetric_key': symmetric_key_b64,  # The actual key material
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
                      invite_id: str, t_ms: int, db: Any) -> str:
    """Create a shareable key_shared event sealed to invite prekey.

    Extracts the invite prekey from the stored invite event and wraps the group key to it.
    Used when sharing network key via invite (sealed to invite prekey for joiner to decrypt).

    Args:
        key_id: Local key event ID (symmetric key to share)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        invite_id: The invite event ID (contains invite_prekey_id and invite_pubkey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        key_shared_id: The stored key_shared event ID
    """
    log.info(f"key_shared.create_for_invite() creating key_shared for key_id={key_id}, invite_id={invite_id[:20]}..., t_ms={t_ms}")

    # Get symmetric key from local key event
    from db import create_unsafe_db
    unsafedb = create_unsafe_db(db)
    key_blob = store.get(key_id, unsafedb)
    if not key_blob:
        raise ValueError(f"key not found: {key_id}")

    key_data = crypto.parse_json(key_blob)
    symmetric_key_b64 = key_data['key']

    # Get invite event to extract prekey info
    invite_blob = store.get(invite_id, unsafedb) # TODO: Make a safedb way to get events like these from the store, or hold the params we need and get the invite_id in the caller so we don't need another lookup 
    if not invite_blob:
        raise ValueError(f"invite not found: {invite_id}")

    invite_data = crypto.parse_json(invite_blob)
    invite_prekey_id = invite_data['invite_prekey_id']
    invite_pubkey_b64 = invite_data['invite_pubkey']

    # Build recipient prekey dict from invite data
    recipient_prekey_dict = {
        'id': crypto.b64decode(invite_prekey_id),
        'public_key': crypto.b64decode(invite_pubkey_b64),
        'type': 'asymmetric'
    }

    log.info(f"key_shared.create_for_invite() extracted invite_prekey_id={invite_prekey_id[:20]}... from invite")

    # Create inner event
    # Note: recipient identity (invite_prekey_id) is in the crypto hint (from wrap()), not in event data
    inner_event_data = {
        'type': 'group_key_shared',
        'key_id': key_id,
        'symmetric_key': symmetric_key_b64,
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign and wrap
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_inner_event = crypto.sign_event(inner_event_data, private_key)
    canonical = crypto.canonicalize_json(signed_inner_event)
    wrapped_blob = crypto.wrap(canonical, recipient_prekey_dict, db)

    # Store with recorded wrapper (for replay)
    # Note: Alice can't decrypt this (only Bob can), so it will remain blocked for Alice
    # But it will still be sent to Bob during sync who can decrypt and project it
    key_shared_id = store.event(wrapped_blob, peer_id, t_ms, db)

    log.info(f"key_shared.create_for_invite() created key_shared_id={key_shared_id}")
    return key_shared_id


def create_for_link_invite(key_id: str, peer_id: str, peer_shared_id: str,
                           link_invite_id: str, t_ms: int, db: Any) -> str:
    """Create a shareable key_shared event sealed to link_invite prekey.

    Similar to create_for_invite, but for multi-device linking.
    Extracts the link prekey from the stored link_invite event.

    Args:
        key_id: Local key event ID (symmetric key to share)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        link_invite_id: The link_invite event ID (contains link_prekey_id and link_pubkey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        key_shared_id: The stored key_shared event ID
    """
    log.info(f"key_shared.create_for_link_invite() creating key_shared for key_id={key_id}, link_invite_id={link_invite_id[:20]}..., t_ms={t_ms}")

    # Get symmetric key from local key event
    unsafedb = create_unsafe_db(db)
    key_blob = store.get(key_id, unsafedb)
    if not key_blob:
        raise ValueError(f"key not found: {key_id}")

    key_data = crypto.parse_json(key_blob)
    symmetric_key_b64 = key_data['key']

    # Get link_invite event to extract prekey info
    link_invite_blob = store.get(link_invite_id, unsafedb)
    if not link_invite_blob:
        raise ValueError(f"link_invite not found: {link_invite_id}")

    link_invite_data = crypto.parse_json(link_invite_blob)
    link_prekey_id = link_invite_data['link_prekey_id']
    link_pubkey_b64 = link_invite_data['link_pubkey']

    # Build recipient prekey dict from link_invite data
    recipient_prekey_dict = {
        'id': crypto.b64decode(link_prekey_id),
        'public_key': crypto.b64decode(link_pubkey_b64),
        'type': 'asymmetric'
    }

    log.info(f"key_shared.create_for_link_invite() extracted link_prekey_id={link_prekey_id[:20]}... from link_invite")

    # Create inner event
    inner_event_data = {
        'type': 'group_key_shared',
        'key_id': key_id,
        'symmetric_key': symmetric_key_b64,
        'created_by': peer_shared_id,
        'created_at': t_ms
    }

    # Sign and wrap
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_inner_event = crypto.sign_event(inner_event_data, private_key)
    canonical = crypto.canonicalize_json(signed_inner_event)
    wrapped_blob = crypto.wrap(canonical, recipient_prekey_dict, db)

    # Store with recorded wrapper (for replay)
    key_shared_id = store.event(wrapped_blob, peer_id, t_ms, db)

    log.info(f"key_shared.create_for_link_invite() created key_shared_id={key_shared_id}")
    return key_shared_id


def project(key_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project key_shared event into keys table and shareable_events."""
    log.warning(f"[GROUP_KEY_SHARED_PROJECT] key_shared_id={key_shared_id[:20]}..., seen_by={recorded_by[:20]}...")

    # Get blob from store (already unwrapped by recorded)
    blob = store.get(key_shared_id, db)
    if not blob:
        log.warning(f"key_shared.project() blob not found for key_shared_id={key_shared_id}")
        return None

    # Unwrap (decrypt) - recorded should have already done this, but we need the plaintext
    plaintext, missing_keys = crypto.unwrap_event(blob, recorded_by, db)
    log.warning(f"[GROUP_KEY_SHARED_PROJECT] unwrap result: plaintext={'YES' if plaintext else 'NO'}, missing_keys={missing_keys}")
    if not plaintext:
        # Can't decrypt - this event is not for us
        # It's already shareable (marked by recorded.py), but we don't project it
        # Examples:
        # - Alice creates group_key_shared wrapped to Bob's invite prekey - Alice can't decrypt
        # - Bob will receive it and decrypt it
        log.info(f"key_shared.project() can't decrypt, event not for us (wrapped to someone else)")
        # Don't mark as valid - we can't use this event
        # recorded.py already handled crypto blocking if needed
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

    log.warning(f"[GROUP_KEY_SHARED_PROJECT] INSERTED key {original_key_id[:20]}... to group_keys for peer {recorded_by[:20]}...")

    # Insert into group_keys_shared table to track this event
    safedb.execute(
        """INSERT OR IGNORE INTO group_keys_shared
           (key_shared_id, original_key_id, created_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            key_shared_id,
            original_key_id,
            event_data['created_by'],
            event_data['created_at'],
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

    # Notify blocked queue - unblock events that were waiting for this key
    import queues
    unblocked_ids = queues.blocked.notify_event_valid(original_key_id, recorded_by, safedb)
    if unblocked_ids:
        log.warning(f"[GROUP_KEY_SHARED_PROJECT] Unblocked {len(unblocked_ids)} events waiting for key {original_key_id[:20]}...")
        # Re-project the unblocked events
        from events.transit import recorded
        recorded.project_ids(unblocked_ids, db)

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
