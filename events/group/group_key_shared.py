"""Key shared event type (shareable symmetric key sealed to recipient prekey)."""

# Registry metadata
EVENT_TYPE = 'group_key_shared'
SHAREABLE = True  # Sealed keys sync to enable group decryption
PROJECTION_TABLE = ('group_keys_shared', 'group_key_shared_id')

# Wire format constants
WIRE_TYPE_CODE = 0x13  # TYPE_GROUP_KEY_SHARED
WIRE_PLAINTEXT_SIZE = 312  # GROUP_KEY_SHARED_PLAINTEXT_SIZE

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from events.group import group_prekey
from events.group import group_key
from events.identity import peer
from events.identity import invite
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp, EmitEvent
from core.projection.apply import register_command_handler

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for group_key_shared event type

def encode_plaintext(
    key_id: bytes,
    symmetric_key: bytes,
    recipient_prekey_id: bytes,
) -> bytes:
    """Encode a group_key_shared payload plaintext (pre-encryption)."""
    wire_format._require_len("key_id", key_id, 16)
    wire_format._require_len("symmetric_key", symmetric_key, wire_format.SECRET_SIZE)
    wire_format._require_len("recipient_prekey_id", recipient_prekey_id, 16)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = key_id
    payload[16:48] = symmetric_key
    payload[48:64] = recipient_prekey_id
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group_key_shared payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"group_key_shared plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {
        "key_id": data[0:16],
        "symmetric_key": data[16:48],
        "recipient_prekey_id": data[48:64],
    }


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt group_key_shared plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"group_key_shared plaintext must be {WIRE_PLAINTEXT_SIZE} bytes")
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "asymmetric":
        raise ValueError("group_key_shared payload requires asymmetric key")
    sealed = crypto.seal(plaintext, key_data["public_key"])
    if len(sealed) != wire_format.PAYLOAD_SIZE - 16:
        raise ValueError("unexpected sealed length for group_key_shared payload")
    payload = key_id + sealed
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to group_key_shared plaintext."""
    if key_data.get("type") != "asymmetric":
        raise ValueError("group_key_shared payload requires asymmetric key")
    sealed = payload[16:]
    return crypto.unseal(sealed, key_data["private_key"])


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a group_key_shared wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    key_id_b64: str,
    symmetric_key_b64: str,
    recipient_prekey_id_b64: str,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    recipient_prekey: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete group_key_shared wire event."""
    key_id = crypto.b64decode(key_id_b64)
    symmetric_key = crypto.b64decode(symmetric_key_b64)
    recipient_prekey_id = crypto.b64decode(recipient_prekey_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_plaintext(
        key_id=key_id,
        symmetric_key=symmetric_key,
        recipient_prekey_id=recipient_prekey_id,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED | wire_format.FLAG_WRAP_ASYM,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_payload(plaintext, recipient_prekey)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode a group_key_shared wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        return None, []
    if header.flags & wire_format.FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_event_key_by_id(key_id, recorded_by, db)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_payload(payload, key_data)
    else:
        plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "key_id": crypto.b64encode(decoded["key_id"]),
        "symmetric_key": crypto.b64encode(decoded["symmetric_key"]),
        "recipient_prekey_id": crypto.b64encode(decoded["recipient_prekey_id"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    return event_data, []


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
    deterministic_blob = group_key.encode_wire_event(
        key=crypto.b64decode(symmetric_key_b64),
        created_at_ms=0,
    )
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

    if not group_key.is_wire_envelope(key_blob):
        raise ValueError("group_key_shared requires wire group_key event")
    key_data = group_key.decode_wire_event(key_blob)
    symmetric_key_b64 = key_data['key']

    # Get recipient's group prekey for wrapping (asymmetric encryption)
    # Uses group_prekey namespace (content layer) not transit_prekey (sync layer)
    recipient_prekey = group_prekey.get_group_prekey_for_peer(recipient_peer_id, peer_id, db)
    if not recipient_prekey:
        raise ValueError(f"No group prekey found for recipient peer: {recipient_peer_id}")

    # Extract recipient_prekey_id for inclusion in signed event data
    recipient_prekey_id = crypto.b64encode(recipient_prekey['id'])

    # Sign the inner event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)

    wrapped_blob = encode_wire_event(
        key_id_b64=key_id,
        symmetric_key_b64=symmetric_key_b64,
        recipient_prekey_id_b64=recipient_prekey_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        recipient_prekey=recipient_prekey,
        private_key=private_key,
    )

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

    if not group_key.is_wire_envelope(key_blob):
        raise ValueError("group_key_shared requires wire group_key event")
    key_data = group_key.decode_wire_event(key_blob)
    symmetric_key_b64 = key_data['key']

    # Get invite event to extract prekey info
    invite_blob = store.get(invite_id, unsafedb) # TODO: Make a safedb way to get events like these from the store, or hold the params we need and get the invite_id in the caller so we don't need another lookup 
    if not invite_blob:
        raise ValueError(f"invite not found: {invite_id}")

    if not invite.is_wire_envelope(invite_blob):
        raise ValueError("group_key_shared requires wire invite event")
    invite_data = invite.decode_wire_event(invite_blob)
    invite_prekey_id = invite_data['invite_prekey_id']
    invite_pubkey_b64 = invite_data['invite_pubkey']

    # Build recipient prekey dict from invite data
    recipient_prekey_dict = {
        'id': crypto.b64decode(invite_prekey_id),
        'public_key': crypto.b64decode(invite_pubkey_b64),
        'type': 'asymmetric'
    }

    log.info(f"key_shared.create_for_invite() extracted invite_prekey_id={invite_prekey_id[:20]}... from invite")

    # Sign and wrap
    private_key = peer.get_private_key(peer_id, peer_id, db)
    wrapped_blob = encode_wire_event(
        key_id_b64=key_id,
        symmetric_key_b64=symmetric_key_b64,
        recipient_prekey_id_b64=invite_prekey_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        recipient_prekey=recipient_prekey_dict,
        private_key=private_key,
    )

    # Store with recorded wrapper (for replay)
    # Note: Alice can't decrypt this (only Bob can), so it will remain blocked for Alice
    # But it will still be sent to Bob during sync who can decrypt and project it
    key_shared_id = store.event(wrapped_blob, peer_id, t_ms, db)

    log.info(f"key_shared.create_for_invite() created key_shared_id={key_shared_id}")
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
    from core import recorded

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


def _handle_retry_pending_name_updates(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Command handler for retry_pending_name_updates."""
    retry_pending_name_updates(recorded_by, db)


# Register command handler at module load
register_command_handler('retry_pending_name_updates', _handle_retry_pending_name_updates)


def share_key_with_group_members(key_id: str, group_id: str, peer_id: str,
                                   peer_shared_id: str, t_ms: int, db: Any,
                                   exclude_user_id: str | None = None) -> list[str]:
    """Create key_shared events for all members of a group.

    IMPORTANT: Server relays are automatically excluded from key distribution.
    They can sync events but cannot decrypt messages.

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
    from events.identity import server_relay

    log.info(f"key_shared.share_key_with_group_members() key={key_id[:20]}..., group={group_id[:20]}..., exclude={exclude_user_id[:20] + '...' if exclude_user_id else None}")

    # Get all members of the group (excluding self and optionally excluded user)
    # NOTE: User-to-peer relationship is in peers_shared table (user_id column)
    safedb = create_safe_db(db, recorded_by=peer_id)
    if exclude_user_id:
        members = safedb.query(
            """SELECT DISTINCT ps.peer_shared_id
               FROM group_members gm
               JOIN peers_shared ps ON gm.user_id = ps.user_id AND ps.recorded_by = gm.recorded_by
               WHERE gm.group_id = ? AND ps.peer_shared_id != ? AND gm.user_id != ? AND gm.recorded_by = ?""",
            (group_id, peer_shared_id, exclude_user_id, peer_id)
        )
    else:
        members = safedb.query(
            """SELECT DISTINCT ps.peer_shared_id
               FROM group_members gm
               JOIN peers_shared ps ON gm.user_id = ps.user_id AND ps.recorded_by = gm.recorded_by
               WHERE gm.group_id = ? AND ps.peer_shared_id != ? AND gm.recorded_by = ?""",
            (group_id, peer_shared_id, peer_id)
        )

    key_shared_ids = []
    skipped_relays = 0
    for i, member in enumerate(members):
        recipient_peer_shared_id = member['peer_shared_id']

        # SECURITY: Never distribute keys to server relays
        # Server relays can sync events but cannot decrypt messages
        if server_relay.is_server_relay(recipient_peer_shared_id, peer_id, db):
            log.info(f"key_shared.share_key_with_group_members() skipping server relay {recipient_peer_shared_id[:20]}...")
            skipped_relays += 1
            continue

        try:
            key_shared_id = create(
                key_id=key_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_peer_id=recipient_peer_shared_id,
                t_ms=t_ms + i + 1,  # Increment timestamp for each member
                db=db
            )
            key_shared_ids.append(key_shared_id)
            log.info(f"key_shared.share_key_with_group_members() created key_shared for {recipient_peer_shared_id}")
        except Exception as e:
            log.warning(f"key_shared.share_key_with_group_members() failed for {recipient_peer_shared_id}: {e}")

    if skipped_relays > 0:
        log.info(f"key_shared.share_key_with_group_members() skipped {skipped_relays} server relay(s)")

    return key_shared_ids
