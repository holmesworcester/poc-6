"""Key shared event type (shareable symmetric key sealed to recipient prekey)."""

# Registry metadata
EVENT_TYPE = 'group_key_shared'
SHAREABLE = True  # Sealed keys sync to enable group decryption
EPHEMERAL = False
PROJECTION_TABLE = ('group_keys_shared', 'group_key_shared_id')

from typing import Any
import logging
from core import crypto
from core import store
from events.group import group_prekey
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp, EmitEvent

log = logging.getLogger(__name__)


# v2 event specification - asymmetrically encrypted, signed by peer_shared
EVENT_SPEC = {
    'encrypted': True,  # Wrapped to recipient's prekey
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},  # No deps - key material is self-contained
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for group_key_shared events.

    On success:
    - Verifies computed key_id matches claimed key_id (security check)
    - Inserts into group_keys_shared table
    - Emits deterministic group_key event with the shared key material
    """
    event_data = ctx.event_data

    # Validate required fields
    key_id = event_data.get('key_id')
    symmetric_key_b64 = event_data.get('symmetric_key')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')
    recipient_prekey_id = event_data.get('recipient_prekey_id')

    if not key_id or not symmetric_key_b64 or not signed_by or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Validate symmetric key can be decoded
    try:
        crypto.b64decode(symmetric_key_b64)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Create deterministic group_key event data
    # This MUST match group_key.create_with_material() blob structure
    group_key_event_data = {
        'type': 'group_key',
        'key': symmetric_key_b64,  # Same key material
    }

    # Verify the computed key_id matches the claimed key_id
    # Security check: prevents sender from claiming wrong key_id
    deterministic_blob = crypto.canonicalize_json(group_key_event_data)
    computed_key_id = crypto.b64encode(crypto.hash(deterministic_blob))
    if computed_key_id != key_id:
        log.error(f"group_key_shared key_id mismatch: computed={computed_key_id[:20]}... claimed={key_id[:20]}...")
        return ProjectorResult(writes=tuple(), valid_event=False)

    # The group_key event is deterministic - same content = same event_id
    # The apply layer will store it and project it
    emit_group_key = EmitEvent(
        event_type='group_key',
        event_data=group_key_event_data,
        peer_id=None,  # Use recorded_by from context
    )

    # Insert into group_keys_shared tracking table
    # Use computed_key_id (not claimed key_id) since that's the actual event hash
    writes = (
        WriteOp(
            op='insert',
            table='group_keys_shared',
            values={
                'key_shared_id': ctx.event_id,
                'original_key_id': computed_key_id,  # The actual deterministic event_id
                'recipient_prekey_id': recipient_prekey_id,  # From signed event data
                'signed_by': signed_by,
                'created_at': created_at,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(
        writes=writes,
        valid_event=True,
        emit_events=(emit_group_key,),
    )


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

    # Get recipient's group prekey for wrapping (asymmetric encryption)
    # Uses group_prekey namespace (content layer) not transit_prekey (sync layer)
    recipient_prekey = group_prekey.get_group_prekey_for_peer(recipient_peer_id, peer_id, db)
    if not recipient_prekey:
        raise ValueError(f"No group prekey found for recipient peer: {recipient_peer_id}")

    # Extract recipient_prekey_id for inclusion in signed event data
    recipient_prekey_id = crypto.b64encode(recipient_prekey['id'])

    # Create the inner event (to be wrapped to recipient's prekey)
    inner_event_data = {
        'type': 'group_key_shared',
        'key_id': key_id,  # Reference to the key being shared
        'symmetric_key': symmetric_key_b64,  # The actual key material
        'signed_by': peer_shared_id,
        'signer_type': 'peer_shared',
        'recipient_prekey_id': recipient_prekey_id,  # Cryptographically bound recipient
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
    from core.db import create_unsafe_db
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
    inner_event_data = {
        'type': 'group_key_shared',
        'key_id': key_id,
        'symmetric_key': symmetric_key_b64,
        'signed_by': peer_shared_id,
        'signer_type': 'peer_shared',
        'recipient_prekey_id': invite_prekey_id,  # Cryptographically bound recipient
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


def project(key_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project key_shared event into keys table and shareable_events."""
    log.debug(f"key_shared.project() key_shared_id={key_shared_id[:20]}..., recorded_by={recorded_by[:20]}...")

    # Get blob from store (already unwrapped by recorded)
    blob = store.get(key_shared_id, db)
    if not blob:
        log.warning(f"key_shared.project() blob not found for key_shared_id={key_shared_id}")
        return None

    # Unwrap (decrypt) - recorded should have already done this, but we need the plaintext
    plaintext, missing_keys = crypto.unwrap_event(blob, recorded_by, db)
    log.debug(f"key_shared.project() unwrap result: plaintext={'YES' if plaintext else 'NO'}, missing_keys={missing_keys}")
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

    # Verify signature - get public key from signed_by peer_shared
    from events.identity import peer_shared
    signed_by = event_data['signed_by']
    public_key = peer_shared.get_public_key(signed_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"key_shared.project() signature verification failed for key_shared_id={key_shared_id}")
        return None

    # Create DETERMINISTIC group_key event from the shared key material
    # This produces the SAME key_id that the creator has (same key material = same hash)
    original_key_id = event_data['key_id']
    symmetric_key = crypto.b64decode(event_data['symmetric_key'])

    # Create deterministic group_key event
    from events.group import group_key
    computed_key_id = group_key.create_with_material(
        symmetric_key,
        recorded_by,
        event_data['created_at'],  # Use sharing event's timestamp for metadata
        db
    )

    # Verify determinism: computed key_id MUST match original
    # Since group_key events are deterministic (content-addressed), a mismatch indicates
    # either corruption or a malicious sender providing wrong key material
    if computed_key_id != original_key_id:
        log.error(f"key_shared.project() key_id mismatch! computed={computed_key_id[:20]}... vs original={original_key_id[:20]}... - rejecting")
        return None

    log.debug(f"key_shared.project() created deterministic key {computed_key_id[:20]}... for peer {recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Extract recipient_prekey_id from blob (first 16 bytes is the hint/prekey_id)
    recipient_prekey_id = crypto.b64encode(blob[:crypto.ID_SIZE])

    # Insert into group_keys_shared table to track this event
    # Store original_key_id for auditing (what sender claimed), but we use computed_key_id
    safedb.execute(
        """INSERT OR IGNORE INTO group_keys_shared
           (key_shared_id, original_key_id, recipient_prekey_id, signed_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            key_shared_id,
            computed_key_id,  # Use computed (deterministic) key_id
            recipient_prekey_id,
            event_data['signed_by'],
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )

    # Mark key_shared event as valid for this peer
    # Note: computed_key_id is already marked valid by group_key.create_with_material()
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (key_shared_id, recorded_by)
    )

    # Notify blocked queue - unblock events that were waiting for this key
    from core import queues
    unblocked_ids = queues.blocked.notify_event_valid(computed_key_id, recorded_by, safedb)
    if unblocked_ids:
        log.info(f"key_shared.project() unblocked {len(unblocked_ids)} events waiting for key {computed_key_id[:20]}...")
        # Re-project the unblocked events
        from events.network import recorded
        recorded.project_ids(unblocked_ids, db)

    # DETERMINISTIC TRIGGER: Retry pending name updates now that key is available
    # This handles username_update and network_name_update creation
    # when the group key needed for encryption wasn't available before
    retry_pending_name_updates(recorded_by, db)

    return key_shared_id


def retry_pending_name_updates(recorded_by: str, db: Any) -> None:
    """Retry creating pending name update events now that group key is available.

    This is called automatically when a group_key_shared event is projected,
    providing a deterministic trigger for retrying pending name updates.

    Args:
        recorded_by: The peer that recorded the key
        db: Database connection
    """
    log.info(f"retry_pending_name_updates() triggered for peer {recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get all pending name updates waiting for key
    pending_items = safedb.query(
        "SELECT * FROM pending_name_updates WHERE status = 'waiting_for_key' AND recorded_by = ?",
        (recorded_by,)
    )

    if not pending_items:
        log.debug(f"retry_pending_name_updates() no pending items")
        return

    log.info(f"retry_pending_name_updates() found {len(pending_items)} pending items")

    from events.identity import username_update, network_name_update, peer_name_update
    from events.network import recorded

    for item in pending_items:
        try:
            item_type = item['type']
            entity_id = item['entity_id']
            name = item['name']
            peer_id = item['peer_id']
            peer_shared_id = item['peer_shared_id']

            log.info(f"retry_pending_name_updates() retrying {item_type} for {entity_id[:20]}...")

            # Try to create the name update event
            if item_type == 'username':
                update_id = username_update.create(
                    user_id=entity_id,
                    name=name,
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    t_ms=item['created_at'],
                    db=db
                )
                # Project immediately so name is available in user_names table
                recorded_id = recorded.create(update_id, peer_id, item['created_at'] + 1, db, return_dupes=True)
                recorded.project_ids([recorded_id], db)
                # Delete from pending
                safedb.execute(
                    "DELETE FROM pending_name_updates WHERE id=? AND recorded_by=?",
                    (item['id'], recorded_by)
                )
                log.info(f"retry_pending_name_updates() successfully created and projected username_update for {entity_id[:20]}...")

            elif item_type == 'network_name':
                update_id = network_name_update.create(
                    network_id=entity_id,
                    name=name,
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    t_ms=item['created_at'],
                    db=db
                )
                # Project immediately so name is available in network_names table
                recorded_id = recorded.create(update_id, peer_id, item['created_at'] + 1, db, return_dupes=True)
                recorded.project_ids([recorded_id], db)
                # Delete from pending
                safedb.execute(
                    "DELETE FROM pending_name_updates WHERE id=? AND recorded_by=?",
                    (item['id'], recorded_by)
                )
                log.info(f"retry_pending_name_updates() successfully created and projected network_name_update for {entity_id[:20]}...")

            elif item_type == 'peer_name':
                update_id = peer_name_update.create(
                    peer_target_id=entity_id,
                    name=name,
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    t_ms=item['created_at'],
                    db=db
                )
                # Project immediately so name is available in peer_names table
                recorded_id = recorded.create(update_id, peer_id, item['created_at'] + 1, db, return_dupes=True)
                recorded.project_ids([recorded_id], db)
                # Delete from pending
                safedb.execute(
                    "DELETE FROM pending_name_updates WHERE id=? AND recorded_by=?",
                    (item['id'], recorded_by)
                )
                log.info(f"retry_pending_name_updates() successfully created and projected peer_name_update for {entity_id[:20]}...")

        except username_update.KeyNotAvailableError as e:
            # Key or group not available yet - keep waiting (don't mark as failed)
            # This will be retried on next group_key_shared or group projection
            log.info(f"retry_pending_name_updates() key/group not ready yet for {item['type']}: {e}")
        except network_name_update.KeyNotAvailableError as e:
            log.info(f"retry_pending_name_updates() key/group not ready yet for {item['type']}: {e}")
        except peer_name_update.KeyNotAvailableError as e:
            log.info(f"retry_pending_name_updates() key/group not ready yet for {item['type']}: {e}")
        except Exception as e:
            # Unexpected error - mark as failed
            log.warning(f"retry_pending_name_updates() unexpected error for {item['type']}: {e}")
            safedb.execute(
                "UPDATE pending_name_updates SET status='failed', error=? WHERE id=? AND recorded_by=?",
                (str(e), item['id'], recorded_by)
            )


def share_key_with_group_members(key_id: str, group_id: str, peer_id: str,
                                   peer_shared_id: str, t_ms: int, db: Any,
                                   exclude_user_id: str | None = None) -> list[str]:
    """Create key_shared events for all members of a group.

    Args:
        key_id: The symmetric key to share
        group_id: Group whose members should receive the key
        peer_id: Local peer ID (creator)
        peer_shared_id: Public peer ID (creator)
        t_ms: Base timestamp
        db: Database connection
        exclude_user_id: Optional user_id to exclude (e.g., removed member)

    Returns:
        List of key_shared event IDs created
    """
    log.info(f"key_shared.share_key_with_group_members() key={key_id[:20]}..., group={group_id[:20]}..., exclude={exclude_user_id[:20] + '...' if exclude_user_id else None}")

    # Get all members of the group (excluding self and optionally excluded user)
    safedb = create_safe_db(db, recorded_by=peer_id)
    if exclude_user_id:
        members = safedb.query(
            """SELECT DISTINCT u.peer_id
               FROM group_members gm
               JOIN users u ON gm.user_id = u.user_id AND u.recorded_by = gm.recorded_by
               WHERE gm.group_id = ? AND u.peer_id != ? AND gm.user_id != ? AND gm.recorded_by = ?""",
            (group_id, peer_shared_id, exclude_user_id, peer_id)
        )
    else:
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
