"""Group member event type (shareable, encrypted) - represents group membership."""

# Registry metadata
EVENT_TYPE = 'group_member'
SHAREABLE = True  # Memberships sync for group access control
PROJECTION_TABLE = ('group_members', 'user_id')

# Wire format constants
WIRE_TYPE_CODE = 0x11  # TYPE_GROUP_MEMBER
WIRE_PLAINTEXT_SIZE = 344  # GROUP_MEMBER_PLAINTEXT_SIZE

from typing import Any
import struct
import logging
from core import crypto
from core import store
from core import wire_format
from events.group import group
from events.identity import peer_shared, network, peer
from core.db import create_safe_db, create_unsafe_db
from core import queues
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for group_member event type

def encode_plaintext(
    group_id: bytes,
    user_id: bytes,
    added_by: bytes,
    admin_grant_id: bytes | None,
) -> bytes:
    """Encode a group_member payload plaintext (pre-encryption).

    Layout (344 bytes):
    - group_id (16)
    - user_id (16)
    - added_by (16)
    - admin_grant_id (16)
    - pad
    """
    wire_format._require_len("group_id", group_id, 16)
    wire_format._require_len("user_id", user_id, 16)
    wire_format._require_len("added_by", added_by, 16)
    admin_grant_bytes = admin_grant_id or (b"\x00" * 16)
    wire_format._require_len("admin_grant_id", admin_grant_bytes, 16)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:16] = group_id
    payload[16:32] = user_id
    payload[32:48] = added_by
    payload[48:64] = admin_grant_bytes
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group_member payload plaintext (post-decryption)."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"group_member plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")
    group_id = data[0:16]
    user_id = data[16:32]
    added_by = data[32:48]
    admin_grant_id = data[48:64]
    if admin_grant_id == b"\x00" * 16:
        admin_grant_id = None
    return {
        "group_id": group_id,
        "user_id": user_id,
        "added_by": added_by,
        "admin_grant_id": admin_grant_id,
    }


def _encrypt_payload(plaintext: bytes, key_data: dict[str, Any]) -> bytes:
    """Encrypt group_member plaintext into wire payload."""
    if len(plaintext) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"group_member plaintext must be {WIRE_PLAINTEXT_SIZE} bytes")
    key_id = wire_format._require_len("key_id", key_data.get("id", b""), 16)
    if key_data.get("type") != "symmetric":
        raise ValueError("group_member payload requires symmetric key")
    nonce = crypto.deterministic_nonce(key_id, plaintext)
    ciphertext = crypto.encrypt(plaintext, key_data["key"], nonce)
    if len(ciphertext) != WIRE_PLAINTEXT_SIZE + 16:
        raise ValueError("unexpected ciphertext length for group_member payload")
    payload = key_id + nonce + ciphertext
    return wire_format._require_len("payload", payload, wire_format.PAYLOAD_SIZE)


def _decrypt_payload(payload: bytes, key_data: dict[str, Any]) -> bytes:
    """Decrypt wire payload to group_member plaintext."""
    if key_data.get("type") != "symmetric":
        raise ValueError("group_member payload requires symmetric key")
    nonce = payload[16:40]
    ciphertext = payload[40:]
    return crypto.decrypt(ciphertext, key_data["key"], nonce)


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a group_member wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    group_id_b64: str,
    user_id_b64: str,
    added_by_b64: str,
    admin_grant_b64: str | None,
    signed_by_b64: str,
    signer_type: str,
    created_at_ms: int,
    key_data: dict[str, Any],
    private_key: bytes,
) -> bytes:
    """Encode a complete group_member wire event."""
    group_id = crypto.b64decode(group_id_b64)
    user_id = crypto.b64decode(user_id_b64)
    added_by = crypto.b64decode(added_by_b64)
    admin_grant_id = crypto.b64decode(admin_grant_b64) if admin_grant_b64 else None
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_plaintext(
        group_id=group_id,
        user_id=user_id,
        added_by=added_by,
        admin_grant_id=admin_grant_id,
    )
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_ENCRYPTED,
        signer_type=wire_format.signer_type_from_str(signer_type),
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    signed_bytes = wire_format._signing_bytes(header, plaintext)
    signature = crypto.sign(signed_bytes, private_key)
    payload = _encrypt_payload(plaintext, key_data)
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(
    data: bytes,
    recorded_by: str,
    db: Any,
    key_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode a group_member wire event."""
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
    if decoded["added_by"] != header.signer_id:
        raise ValueError("added_by does not match signer_id")
    event_data = {
        "type": EVENT_TYPE,
        "group_id": crypto.b64encode(decoded["group_id"]),
        "user_id": crypto.b64encode(decoded["user_id"]),
        "added_by": crypto.b64encode(decoded["added_by"]),
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
            'fields': ['group_id'],
        },
        'user': {
            'source': 'table',
            'table': 'users',
            'key': 'user_id',
            'fields': ['user_id'],
        },
        'adder': {
            'source': 'table',
            'table': 'peers_shared',
            'key': 'peer_shared_id',
            'key_from': 'added_by',
            'fields': ['user_id'],
        },
        'admin_grant': {
            'source': 'table',
            'table': 'admins',
            'key': 'admin_id',
            'key_from': 'admin_grant',
            'fields': ['user_id'],
        },
    },
    'optional': {},
    'cascade_on_delete': [],
}

def validate(group_id: str, added_by: str, recorded_by: str, db: Any) -> bool:
    """Validate that added_by has authorization to add members to the group.

    Authorization rule:
    - added_by must have an admin event in the admins table

    Uses centralized is_admin() function from events.identity.invite.

    Args:
        group_id: Group to check membership for
        added_by: peer_shared_id attempting to add members
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if authorized, False otherwise
    """
    from events.identity import invite
    return invite.is_admin(added_by, recorded_by, db)


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for group_member events."""
    event_data = ctx.event_data

    group_id = event_data.get('group_id')
    user_id = event_data.get('user_id')
    added_by = event_data.get('added_by')
    signed_by = event_data.get('signed_by')
    admin_grant = event_data.get('admin_grant')
    created_at = event_data.get('created_at')

    if not all([group_id, user_id, added_by, signed_by, admin_grant, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_group_member(group_id, user_id, added_by, admin_grant)

    if added_by != signed_by:
        return ProjectorResult(writes=tuple(), valid_event=False)

    adder_row = ctx.deps.get('adder') or {}
    admin_row = ctx.deps.get('admin_grant') or {}
    adder_user_id = adder_row.get('user_id')
    admin_user_id = admin_row.get('user_id')

    if not adder_user_id or not admin_user_id or adder_user_id != admin_user_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='group_members',
            values={
                'member_id': ctx.event_id,
                'group_id': group_id,
                'user_id': user_id,
                'added_by': added_by,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_group_member(
    group_id: str,
    user_id: str,
    added_by: str,
    admin_grant: str | None,
) -> None:
    """Validate group_member fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        group_id=crypto.b64decode(group_id),
        user_id=crypto.b64decode(user_id),
        added_by=crypto.b64decode(added_by),
        admin_grant_id=crypto.b64decode(admin_grant) if admin_grant else None,
    )
    decoded = decode_plaintext(plaintext)
    if decoded["group_id"] != crypto.b64decode(group_id):
        raise ValueError("wire shadow decode group_id mismatch")


def create(group_id: str, user_id: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any,
           admin_grant: str | None = None) -> str:
    """Create a group_member event to add a user to a group.

    Only admins can add new members to groups.
    Automatically shares group key with the new member.

    Args:
        group_id: Group to add member to
        user_id: User (peer_shared_id) to add to the group
        peer_id: Local peer ID (for signing and seeing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection
        admin_grant: Optional admin_id that grants authority to add members.
                    If provided, used directly. If None, looked up from admins table.

    Returns:
        member_id: The stored group_member event ID
    """
    log.info(f"group_member.create() adding user={user_id} to group={group_id} by {peer_shared_id}")

    # Use SafeDB for peer-scoped queries
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Validate group exists
    group_row = group.get(group_id, peer_id, db)
    if not group_row:
        raise ValueError(f"Group {group_id} not found")

    # Check authorization - caller must be admin to add members
    if not validate(group_id, peer_shared_id, peer_id, db):
        raise ValueError(f"User {peer_shared_id} not authorized to add members to group {group_id} (only admins can add members)")

    # Get admin_grant for the adding user (explicit dependency for convergence)
    # This is REQUIRED for events that need admin authorization to project correctly
    # when replayed in different orders on receiving peers.
    admin_grant_id = admin_grant  # Use passed-in value if provided

    if not admin_grant_id:
        # Look up admin_grant from admins table
        # Get adder's user_id
        adder_user_id = peer_shared.get_user_id(peer_shared_id, peer_id, db)
        if adder_user_id:
            # Get network_id
            network_id = network.get_network_id(peer_id, db)
            if network_id:
                from events.identity import admin as admin_module
                admin_grant_id = admin_module.my_grant(adder_user_id, network_id, peer_id, db)

    if admin_grant_id:
        log.info(f"group_member.create() including admin_grant={admin_grant_id[:20]}...")
    else:
        log.warning(f"group_member.create() NO admin_grant found - event may fail projection on receivers!")

    _wire_shadow_group_member(group_id, user_id, peer_shared_id, admin_grant_id)

    private_key = peer.get_private_key(peer_id, peer_id, db)
    key_data = group.pick_key(group_id, peer_id, db)
    blob = encode_wire_event(
        group_id_b64=group_id,
        user_id_b64=user_id,
        added_by_b64=peer_shared_id,
        admin_grant_b64=admin_grant_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )
    member_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_member.create() created member_id={member_id}")

    # Share group key with new member
    # Get the new member's peer_shared_id from peers_shared (user→peer is one-to-many)
    member_peer = peer_shared.get_for_user(user_id, peer_id, db)

    if member_peer:
        from events.group import group_key_shared
        try:
            group_key_shared.create(
                key_id=group_row['key_id'],
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_peer_id=member_peer['peer_shared_id'],
                t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                db=db
            )
            log.info(f"group_member.create() shared key with new member {user_id}")
        except Exception as e:
            log.warning(f"group_member.create() failed to share key with {user_id}: {e}")

    # Per design doc: Share key to all active device links for this user
    # Any new group memberships must be sealed to all active device links
    # This ensures all devices of a user can decrypt groups they're added to
    from events.group import group_key_shared

    # Get all active device links for this user (peer_shared entries with matching user_id)
    other_devices = safedb.query(
        """SELECT peer_shared_id FROM peers_shared
           WHERE user_id = ? AND recorded_by = ? AND peer_shared_id != ?""",
        (user_id, peer_id, member_peer['peer_shared_id'] if member_peer else None)
    )

    for device_row in other_devices:
        other_peer_shared_id = device_row['peer_shared_id']
        try:
            # Share key with other device by creating group_key_shared sealed to their identity
            group_key_shared.create(
                key_id=group_row['key_id'],
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_peer_id=other_peer_shared_id,
                t_ms=t_ms,  # No offset needed - DAG deps handle ordering
                db=db
            )
            log.info(f"group_member.create() shared key to device link {other_peer_shared_id[:20]}...")
        except Exception as e:
            log.warning(f"group_member.create() failed to share key to device link {other_peer_shared_id[:20]}...: {e}")

    return member_id


def is_member(user_id: str, group_id: str, recorded_by: str, db: Any) -> bool:
    """Check if a user is a member of a group.

    Args:
        user_id: User's ID to check
        group_id: Group to check membership in
        recorded_by: Perspective of which peer is checking
        db: Database connection

    Returns:
        True if user is a member, False otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Check if user is a member of the group
    member = safedb.query_one(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ? AND recorded_by = ?",
        (group_id, user_id, recorded_by)
    )

    return member is not None


def list_members(group_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all members of a group (excluding removed users).

    Args:
        group_id: Group to list members for
        recorded_by: Perspective of which peer is querying
        db: Database connection

    Returns:
        List of member dicts with user_id, name, added_by, created_at
        (name is preferentially from user_names table (encrypted username), falls back to users.name)
        Removed users are filtered out.
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Query group_members table with user names joined
    # Prefer encrypted username from user_names table if available, else fall back to plaintext from users table
    # Filter out removed users by LEFT JOIN on removed_users and checking for NULL
    return safedb.query(
        """SELECT gm.user_id, COALESCE(un.name, u.name) as name, gm.added_by, gm.created_at
           FROM group_members gm
           JOIN users u ON gm.user_id = u.user_id AND gm.recorded_by = u.recorded_by
           LEFT JOIN user_names un ON gm.user_id = un.user_id AND gm.recorded_by = un.recorded_by
           LEFT JOIN removed_users ru ON gm.user_id = ru.user_id AND gm.recorded_by = ru.recorded_by
           WHERE gm.group_id = ? AND gm.recorded_by = ? AND ru.user_id IS NULL
           ORDER BY gm.created_at ASC""",
        (group_id, recorded_by)
    )
