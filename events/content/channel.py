"""Channel event type (shareable, encrypted)."""
from __future__ import annotations

# Registry metadata
EVENT_TYPE = 'channel'
SHAREABLE = True  # Channels sync to all members
PROJECTION_TABLE = ('channels', 'channel_id')

# Wire format constants
WIRE_TYPE_CODE = 0x02  # TYPE_CHANNEL
WIRE_PLAINTEXT_SIZE = 344  # CHANNEL_PLAINTEXT_SIZE
NAME_MAX = 64

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from events.group import group_key, group as group_module, group_member
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for channel event type

def encode_plaintext(
    group_id: bytes,
    name: str | bytes,
    disappearing_time_ms: int,
    is_main: int | bool,
    admin_grant_id: bytes | None,
) -> bytes:
    """Encode a channel payload plaintext (pre-encryption).

    Layout (344 bytes):
    - group_id (16)
    - name_len (u16)
    - name_bytes (NAME_MAX)
    - disappearing_time_ms (u64)
    - is_main (u8)
    - admin_grant_id (16, zero if none)
    - pad
    """
    wire_format._require_len("group_id", group_id, 16)

    if isinstance(name, str):
        name_bytes = name.encode("utf-8")
    else:
        name_bytes = bytes(name)

    if len(name_bytes) > NAME_MAX:
        raise ValueError(f"name exceeds {NAME_MAX} bytes, got {len(name_bytes)}")

    admin_grant_bytes = admin_grant_id or (b"\x00" * 16)
    wire_format._require_len("admin_grant_id", admin_grant_bytes, 16)

    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = group_id
    struct.pack_into("<H", payload, 16, len(name_bytes))
    payload[18:18 + len(name_bytes)] = name_bytes
    struct.pack_into("<Q", payload, 18 + NAME_MAX, disappearing_time_ms)
    payload[26 + NAME_MAX] = 1 if is_main else 0
    payload[27 + NAME_MAX:27 + NAME_MAX + 16] = admin_grant_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a channel payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"channel plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")

    group_id = data[0:16]
    (name_len,) = struct.unpack_from("<H", data, 16)
    if name_len > NAME_MAX:
        raise ValueError(f"name_len exceeds {NAME_MAX}, got {name_len}")

    name_bytes = data[18:18 + name_len]
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("name is not valid utf-8") from exc

    (disappearing_time_ms,) = struct.unpack_from("<Q", data, 18 + NAME_MAX)
    is_main = data[26 + NAME_MAX]
    admin_grant_id = data[27 + NAME_MAX:27 + NAME_MAX + 16]
    if admin_grant_id == b"\x00" * 16:
        admin_grant_id = None

    return {
        "group_id": group_id,
        "name": name,
        "disappearing_time_ms": disappearing_time_ms,
        "is_main": is_main,
        "admin_grant_id": admin_grant_id,
    }


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt channel plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"channel plaintext must be {WIRE_PLAINTEXT_SIZE} bytes")
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("channel payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != WIRE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for channel payload")
    payload = key_id + nonce + ciphertext
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to channel plaintext."""
    if key_data.get("type") != "symmetric":
        raise ValueError("channel payload requires symmetric key")
    key_id = payload[:16]
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def encode_wire_event(
    *,
    group_id_b64: str,
    name: str,
    disappearing_time_ms: int,
    is_main: int | bool,
    admin_grant_b64: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete channel wire event."""
    group_id = crypto.b64decode(group_id_b64)
    signer_id = crypto.b64decode(signed_by_b64)
    admin_grant_id = crypto.b64decode(admin_grant_b64) if admin_grant_b64 else None

    plaintext = encode_plaintext(
        group_id=group_id,
        name=name,
        disappearing_time_ms=disappearing_time_ms,
        is_main=is_main,
        admin_grant_id=admin_grant_id,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=disappearing_time_ms,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_payload(plaintext, key_data)
    return wire_format.build_envelope(header, payload, signature)


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a channel wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def decode_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode a channel wire event."""
    header, payload, signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        return None, []
    if header.flags & wire_format.FLAG_ENCRYPTED:
        key_id = payload[:16]
        key_id_b64 = crypto.b64encode(key_id)
        key_data = crypto.get_key_by_id(key_id, recorded_by, db, key_cache=key_cache)
        if not key_data:
            return None, [key_id_b64]
        plaintext = _decrypt_payload(payload, key_data)
    else:
        plaintext = payload[:WIRE_PLAINTEXT_SIZE]

    decoded = decode_plaintext(plaintext)
    event_data = {
        "type": EVENT_TYPE,
        "group_id": crypto.b64encode(decoded["group_id"]),
        "name": decoded["name"],
        "disappearing_time_ms": decoded["disappearing_time_ms"],
        "is_main": decoded["is_main"],
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
        "_wire_signature": signature,
        "_wire_signed_bytes": wire_format._signing_bytes(header, plaintext),
    }
    if decoded["admin_grant_id"]:
        event_data["admin_grant"] = crypto.b64encode(decoded["admin_grant_id"])
    return event_data, []


# v2 event specification - signed by peer_shared, encrypted
EVENT_SPEC = {
    'encrypted': True,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'group': {
            'source': 'table',
            'table': 'groups',
            'key': 'group_id',
            'fields': ['group_id', 'key_id'],
        },
    },
    'optional': {
        'admin_grant': {
            'source': 'table',
            'table': 'admins',
            'key': 'admin_id',
            'key_from': 'admin_grant',
            'fields': ['user_id', 'admin_id'],
            'required_if_present': True,  # If admin_grant field present, must be valid
        },
    },
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for channel events."""
    event_data = ctx.event_data

    name = event_data.get('name')
    group_id = event_data.get('group_id')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')
    disappearing_time_ms = event_data.get('disappearing_time_ms', 0) or 0
    is_main = event_data.get('is_main', 0) or 0
    admin_grant = event_data.get('admin_grant')

    # Validate required fields
    if not name or not group_id or not signed_by or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_channel(
        group_id=group_id,
        name=name,
        disappearing_time_ms=disappearing_time_ms,
        is_main=is_main,
        admin_grant=admin_grant,
    )

    # Verify group exists
    group_row = ctx.deps.get('group')
    if not group_row or not group_row.get('group_id'):
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Verify admin authorization if admin_grant present
    if admin_grant:
        admin_row = ctx.deps.get('admin_grant')
        if not admin_row:
            # admin_grant specified but not valid - reject
            return ProjectorResult(writes=tuple(), valid_event=False)
        # Verify signer is authorized by admin_grant
        signer_info = ctx.signer
        if signer_info and signer_info.get('user_id'):
            if admin_row.get('user_id') != signer_info['user_id']:
                # Admin grant doesn't match signer
                return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='channels',
            values={
                'channel_id': ctx.event_id,
                'name': name,
                'group_id': group_id,
                'signed_by': signed_by,
                'created_at': created_at,
                'disappearing_time_ms': disappearing_time_ms,
                'is_main': is_main,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_channel(
    group_id: str,
    name: str,
    disappearing_time_ms: int,
    is_main: int,
    admin_grant: str | None,
) -> None:
    """Validate channel fields against the fixed-size wire payload layout."""
    group_id_bytes = crypto.b64decode(group_id)
    admin_grant_bytes = crypto.b64decode(admin_grant) if admin_grant else None
    plaintext = encode_plaintext(
        group_id=group_id_bytes,
        name=name,
        disappearing_time_ms=disappearing_time_ms,
        is_main=is_main,
        admin_grant_id=admin_grant_bytes,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["name"] != name:
        raise ValueError("wire shadow decode name mismatch")
    if decoded["disappearing_time_ms"] != disappearing_time_ms:
        raise ValueError("wire shadow decode disappearing_time_ms mismatch")
    if decoded["is_main"] != (1 if is_main else 0):
        raise ValueError("wire shadow decode is_main mismatch")


def _validate_admin(peer_shared_id: str, recorded_by: str, db: Any) -> bool:
    """Validate that peer_shared_id is an admin.

    An admin is a member of the network's admins group OR the first_peer (network creator).
    Uses centralized is_admin() function from events.identity.invite.

    Args:
        peer_shared_id: Public peer ID to check
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if user is admin, False otherwise
    """
    from events.identity import invite
    return invite.is_admin(peer_shared_id, recorded_by, db)


def create(name: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any,
           group_id: str | None = None, member_user_ids: list[str] | None = None,
           key_id: str | None = None, is_main: bool = False,
           disappearing_time_ms: int = 0, admin_grant: str | None = None) -> str:
    """Create a shareable, encrypted channel event.

    Only admins can create channels (except during initial network setup where group_id is explicitly provided).
    Channels can be:
    - Public: belong to the main group (if group_id=None, will use main group)
    - Private: belong to a dedicated group with specified members (if member_user_ids provided)

    Note: peer_id (local) signs and sees the event; peer_shared_id (public) is the creator identity.

    SECURITY: This function trusts that peer_id is correct and owned by the caller.
    In production, the API authentication layer should validate that the authenticated session
    owns this peer_id before calling this function. This is safe for local-only apps where
    the user controls all peers on the device.

    Args:
        name: Channel name
        peer_id: Local peer ID (for signing and seeing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection
        group_id: Optional explicit group ID (if None and no member_user_ids, will use main group)
        member_user_ids: If provided, create private channel for these members (creates new group)
        key_id: Optional explicit key_id (only used if group_id provided, otherwise derived)
        is_main: True if this is the main channel for a group
        disappearing_time_ms: Messages expire after this duration in ms (0 = permanent, >0 = milliseconds)
        admin_grant: Optional admin_id that grants authority to create channels.
                    If provided, used directly. If None, looked up from admins table.

    Returns:
        channel_id: The created channel event ID
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Validate disappearing_time_ms
    if disappearing_time_ms < 0:
        raise ValueError("disappearing_time_ms must be non-negative")

    # Check authorization - only admins can create channels
    admin_grant_id = admin_grant  # Use passed-in value if provided

    if not _validate_admin(peer_shared_id, peer_id, db):
        raise ValueError(f"User {peer_shared_id} not authorized to create channels (only admins can)")

    # Get admin_grant for inclusion in event (REQUIRED for convergence)
    # This is needed whether group_id is explicit or not
    if not admin_grant_id:
        # Look up admin_grant from admins table
        from events.identity import admin as admin_module

        # Get user_id for this peer_shared_id
        creator_user_id = None
        peer_row = safedb.query_one(
            "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
            (peer_shared_id, peer_id)
        )
        if peer_row and peer_row['user_id']:
            creator_user_id = peer_row['user_id']

        if creator_user_id:
            # Get network_id for admin lookup
            network_row = safedb.query_one(
                "SELECT network_id FROM networks WHERE recorded_by = ? LIMIT 1",
                (peer_id,)
            )
            if network_row:
                admin_grant_id = admin_module.my_grant(creator_user_id, network_row['network_id'], peer_id, db)

    if admin_grant_id:
        log.info(f"channel.create() including admin_grant={admin_grant_id[:20]}...")
    else:
        log.warning(f"channel.create() NO admin_grant found - event may fail projection on receivers!")

    # Determine group_id and key_id
    if member_user_ids:
        # Private channel: create a new group with specified members
        log.info(f"channel.create() creating PRIVATE channel name='{name}', peer_id={peer_id}, members={len(member_user_ids)}")

        # Create group for the private channel
        private_group_id, private_key_id = group_module.create(
            name=f"private_channel_{name}",
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,
            db=db,
            is_main=False
        )

        group_id = private_group_id
        key_id = private_key_id

        # First, ensure the creator is added to the private channel group
        # Get creator's user_id from peers_shared (user→peer relationship stored there)
        peer_row = safedb.query_one(
            "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
            (peer_shared_id, peer_id)
        )
        if not peer_row or not peer_row['user_id']:
            raise ValueError(f"Creator user not found for peer_shared_id {peer_shared_id}")

        creator_user_id = peer_row['user_id']

        # Track which users we've already added
        added_user_ids = set()

        # Add creator first
        try:
            group_member.create(
                group_id=group_id,
                user_id=creator_user_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                db=db
            )
            log.info(f"channel.create() added creator {creator_user_id} to private channel group")
            added_user_ids.add(creator_user_id)
        except Exception as e:
            log.warning(f"channel.create() warning: could not add creator {creator_user_id}: {e}")

        # Add specified members to the private channel group
        # Members should already have prekeys available since they're part of the network
        for user_id in member_user_ids:
            if user_id in added_user_ids:
                continue  # Skip if already added
            try:
                group_member.create(
                    group_id=group_id,
                    user_id=user_id,
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                    db=db
                )
                log.info(f"channel.create() added member {user_id} to private channel group")
                added_user_ids.add(user_id)
            except Exception as e:
                log.warning(f"channel.create() warning: could not immediately add member {user_id} (may need key sharing later): {e}")
                # Don't raise - members can be added later via add_member_to_channel()

        # Add all admins to the private channel group
        # Admins should be in the network and have prekeys
        admins = _get_admin_user_ids(peer_id, db)
        for admin_user_id in admins:
            if admin_user_id in added_user_ids:
                continue  # Skip if already added
            try:
                group_member.create(
                    group_id=group_id,
                    user_id=admin_user_id,
                    peer_id=peer_id,
                    peer_shared_id=peer_shared_id,
                    t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                    db=db
                )
                log.info(f"channel.create() added admin {admin_user_id} to private channel group")
                added_user_ids.add(admin_user_id)
            except Exception as e:
                log.warning(f"channel.create() warning: could not add admin {admin_user_id} (may need key sharing later): {e}")
    else:
        # Public channel: use main group
        if not group_id:
            # Find the main group
            main_group = safedb.query_one(
                "SELECT group_id, key_id FROM groups WHERE is_main = 1 AND recorded_by = ? LIMIT 1",
                (peer_id,)
            )
            if not main_group:
                raise ValueError("No main group found - cannot create public channel")
            group_id = main_group['group_id']
            key_id = main_group['key_id']
        elif not key_id:
            # If group_id provided but no key_id, look it up
            group_row = safedb.query_one(
                "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ?",
                (group_id, peer_id)
            )
            if not group_row:
                raise ValueError(f"Group {group_id} not found")
            key_id = group_row['key_id']

        log.info(f"channel.create() creating PUBLIC channel name='{name}', group_id={group_id}, peer_id={peer_id}, is_main={is_main}")

    _wire_shadow_channel(
        group_id=group_id,
        name=name,
        disappearing_time_ms=disappearing_time_ms,
        is_main=1 if is_main else 0,
        admin_grant=admin_grant_id,
    )

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Get key_data for encryption
    key_data = group_key.get_key(key_id, peer_id, db)

    blob = encode_wire_event(
        group_id_b64=group_id,
        name=name,
        disappearing_time_ms=disappearing_time_ms,
        is_main=1 if is_main else 0,
        admin_grant_b64=admin_grant_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )

    # Store event with recorded wrapper and projection
    event_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"channel.create() created channel_id={event_id}")
    return event_id


def _get_admin_user_ids(recorded_by: str, db: Any) -> list[str]:
    """Get all admin user IDs from the admins table.

    Uses the event-sourced admins table rather than group membership.

    Args:
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        List of admin user_ids
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Query admins table directly (event-sourced admin grants)
    admin_rows = safedb.query(
        "SELECT DISTINCT user_id FROM admins WHERE recorded_by = ?",
        (recorded_by,)
    )

    return [row['user_id'] for row in admin_rows]


def list(recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all channels for a specific peer."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    name_subquery = (
        "SELECT cu.new_channel_name FROM channel_updates cu "
        "WHERE cu.channel_id = c.channel_id AND cu.recorded_by = c.recorded_by "
        "AND cu.new_channel_name IS NOT NULL "
        "ORDER BY cu.global_count DESC, cu.update_id DESC LIMIT 1"
    )
    ttl_subquery = (
        "SELECT cu.new_disappearing_time_ms FROM channel_updates cu "
        "WHERE cu.channel_id = c.channel_id AND cu.recorded_by = c.recorded_by "
        "AND cu.new_disappearing_time_ms IS NOT NULL "
        "ORDER BY cu.global_count DESC, cu.update_id DESC LIMIT 1"
    )
    return safedb.query(
        f"""SELECT c.channel_id,
                   COALESCE(({name_subquery}), c.name) AS name,
                   c.group_id,
                   c.signed_by,
                   c.created_at,
                   COALESCE(({ttl_subquery}), c.disappearing_time_ms) AS disappearing_time_ms
           FROM channels c
           WHERE c.recorded_by = ?
           ORDER BY created_at DESC""",
        (recorded_by,)
    )


def list_with_keys(recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all channels with their current group keys.

    Returns channel info plus the current key_id from the associated group,
    avoiding N+1 queries when displaying key state.

    Args:
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        List of dicts with channel_id, name, group_id, key_id, etc.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    name_subquery = (
        "SELECT cu.new_channel_name FROM channel_updates cu "
        "WHERE cu.channel_id = c.channel_id AND cu.recorded_by = c.recorded_by "
        "AND cu.new_channel_name IS NOT NULL "
        "ORDER BY cu.global_count DESC, cu.update_id DESC LIMIT 1"
    )
    ttl_subquery = (
        "SELECT cu.new_disappearing_time_ms FROM channel_updates cu "
        "WHERE cu.channel_id = c.channel_id AND cu.recorded_by = c.recorded_by "
        "AND cu.new_disappearing_time_ms IS NOT NULL "
        "ORDER BY cu.global_count DESC, cu.update_id DESC LIMIT 1"
    )
    return safedb.query(
        f"""SELECT c.channel_id,
                  COALESCE(({name_subquery}), c.name) AS name,
                  c.group_id,
                  c.signed_by,
                  c.created_at,
                  COALESCE(({ttl_subquery}), c.disappearing_time_ms) AS disappearing_time_ms,
                  g.key_id
           FROM channels c
           LEFT JOIN groups g ON c.group_id = g.group_id AND c.recorded_by = g.recorded_by
           WHERE c.recorded_by = ?
           ORDER BY c.created_at DESC""",
        (recorded_by,)
    )


def get(channel_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get a single channel by ID.

    Args:
        channel_id: Channel ID to look up
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        Channel dict with channel_id, name, group_id, etc., or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    name_subquery = (
        "SELECT cu.new_channel_name FROM channel_updates cu "
        "WHERE cu.channel_id = c.channel_id AND cu.recorded_by = c.recorded_by "
        "AND cu.new_channel_name IS NOT NULL "
        "ORDER BY cu.global_count DESC, cu.update_id DESC LIMIT 1"
    )
    ttl_subquery = (
        "SELECT cu.new_disappearing_time_ms FROM channel_updates cu "
        "WHERE cu.channel_id = c.channel_id AND cu.recorded_by = c.recorded_by "
        "AND cu.new_disappearing_time_ms IS NOT NULL "
        "ORDER BY cu.global_count DESC, cu.update_id DESC LIMIT 1"
    )
    return safedb.query_one(
        f"""SELECT c.channel_id,
                   COALESCE(({name_subquery}), c.name) AS name,
                   c.group_id,
                   c.signed_by,
                   c.created_at,
                   COALESCE(({ttl_subquery}), c.disappearing_time_ms) AS disappearing_time_ms
           FROM channels c
           WHERE c.channel_id = ? AND c.recorded_by = ?""",
        (channel_id, recorded_by)
    )


def add_member_to_channel(channel_id: str, user_id: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any) -> str:
    """Add a member to a private channel (admin-only).

    This adds the user as a member of the channel's underlying group, giving them
    access to decrypt messages in that channel.

    Args:
        channel_id: Channel to add member to
        user_id: User (peer_shared_id) to add
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        member_id: The created group_member event ID

    Raises:
        ValueError: If channel not found, user not admin, or operation fails
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Check authorization - only admins can add members
    if not _validate_admin(peer_shared_id, peer_id, db):
        raise ValueError(f"User {peer_shared_id} not authorized to add members (only admins can)")

    # Get the channel to find its group_id
    channel = safedb.query_one(
        "SELECT group_id FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (channel_id, peer_id)
    )
    if not channel:
        raise ValueError(f"Channel {channel_id} not found")

    group_id = channel['group_id']

    log.info(f"add_member_to_channel() adding user={user_id} to channel={channel_id}, group={group_id}")

    # Add the user as a member of the group
    member_id = group_member.create(
        group_id=group_id,
        user_id=user_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,
        db=db
    )

    log.info(f"add_member_to_channel() added member_id={member_id}")
    return member_id


def get_next_update_count(channel_id: str, recorded_by: str, db: Any) -> int:
    """Get the next global_count for a channel update.

    Returns max existing global_count + 1, or 1 if no updates exist.

    Args:
        channel_id: Channel ID to check
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        Next global_count value to use
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    max_count_row = safedb.query_one(
        "SELECT MAX(global_count) as max_count FROM channel_updates WHERE channel_id = ? AND recorded_by = ?",
        (channel_id, recorded_by)
    )
    return (max_count_row['max_count'] or 0) + 1 if max_count_row else 1
