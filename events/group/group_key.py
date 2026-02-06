"""Group key event type (subjective symmetric keys for network/group content encryption)."""

# Registry metadata
EVENT_TYPE = 'group_key'
SHAREABLE = False  # Local-only - contains symmetric key material
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x12  # TYPE_GROUP_KEY
WIRE_PLAINTEXT_SIZE = 344  # GROUP_KEY_PLAINTEXT_SIZE

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for group_key event type

def encode_plaintext(key: bytes) -> bytes:
    """Encode a group_key payload plaintext.

    Layout (344 bytes):
    - key (32 bytes SECRET_SIZE)
    - pad (312 bytes)
    """
    wire_format._require_len("key", key, wire_format.SECRET_SIZE)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:wire_format.SECRET_SIZE] = key
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group_key payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"group_key plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")
    return {"key": data[0:wire_format.SECRET_SIZE]}


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a group_key wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(*, key: bytes, created_at_ms: int) -> bytes:
    """Encode a complete group_key wire event."""
    plaintext = encode_plaintext(key=key)
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_UNSIGNED,
        signer_type=wire_format.SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * wire_format.SIGNER_ID_SIZE,
    )
    payload = wire_format._pad_payload(plaintext)
    signature = b"\x00" * wire_format.SIGNATURE_SIZE
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a group_key wire event."""
    header, payload, _signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for group_key")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    return {
        "type": "group_key",
        "key": crypto.b64encode(decoded["key"]),
        "created_at": header.created_at_ms,
    }


EVENT_SPEC = {
    'encrypted': False,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def create(peer_id: str, t_ms: int, db: Any) -> str:
    """Create a group key for network content encryption, owned by peer_id."""
    log.info(f"group_key.create() creating new group key for peer_id={peer_id}, t_ms={t_ms}")

    # Generate symmetric key
    key = crypto.generate_secret()

    # Create DETERMINISTIC event blob - only type and key
    # This ensures same key material = same key_id on all peers
    _wire_shadow_group_key(crypto.b64encode(key))

    blob = encode_wire_event(
        key=key,
        created_at_ms=0,
    )

    # Store event with recorded wrapper and projection
    # t_ms is used for recorded_at metadata, not in the blob itself
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_key.create() created key_id={key_id}")
    return key_id


def create_with_material(key_material: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create group key event with provided key material (for invite group keys).

    Creates a DETERMINISTIC group_key event from the key material.
    Same key_material = same key_id on all peers.

    Args:
        key_material: The symmetric key bytes
        peer_id: Peer ID that owns this key
        t_ms: Timestamp (used for recorded_at, NOT in the blob)
        db: Database connection

    Returns:
        Event ID (to use as hint when wrapping)
    """
    log.info(f"group_key.create_with_material() creating key for peer_id={peer_id}, t_ms={t_ms}")

    # Create DETERMINISTIC event blob - only type and key
    # This ensures same key material = same key_id on all peers
    _wire_shadow_group_key(crypto.b64encode(key_material))

    blob = encode_wire_event(
        key=key_material,
        created_at_ms=0,
    )
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_key.create_with_material() created key_id={key_id}")
    return key_id


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for group_key events."""
    event_data = ctx.event_data

    if event_data.get('type') != 'group_key':
        return ProjectorResult(writes=tuple(), valid_event=False)

    key_b64 = event_data.get('key')
    if not key_b64:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_group_key(key_b64)

    try:
        key_bytes = crypto.b64decode(key_b64)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='group_keys',
            values={
                'key_id': ctx.event_id,
                'key': key_bytes,
                'created_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_group_key(key_b64: str) -> None:
    """Validate group_key fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(key=crypto.b64decode(key_b64))
    decoded = decode_plaintext(plaintext)
    if decoded["key"] != crypto.b64decode(key_b64):
        raise ValueError("wire shadow decode key mismatch")


def get_key(key_id: str, recorded_by: str, db: Any) -> dict[str, Any]:
    """Get group key from database in format expected by crypto.wrap().

    Args:
        key_id: Base64-encoded key ID (event ID)
        recorded_by: Peer ID requesting access
        db: Database connection

    Returns:
        Key dict for crypto.wrap()

    Raises:
        ValueError: If key not found in group_keys table
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT key FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (key_id, recorded_by)
    )
    if not row:
        raise ValueError(f"group key not found: {key_id}")

    return {
        'id': crypto.b64decode(key_id),  # Event ID as hint
        'key': row['key'],  # Already bytes from DB
        'type': 'symmetric'
    }


def get_or_create_clean_key(group_id: str, peer_id: str, t_ms: int, db: Any) -> str:
    """Get an existing clean key or create a new one if needed.

    A "clean" key is one NOT in the keys_to_purge table (not encrypting deleted messages).
    Used during forward secrecy rekeying to find a key safe to re-encrypt with.

    Args:
        group_id: Group that owns the key
        peer_id: Local peer ID
        t_ms: Timestamp for creating new key if needed
        db: Database connection

    Returns:
        key_id: A clean group key suitable for rekeying

    Raises:
        ValueError: If no group key exists and cannot create one
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Find an existing clean key (not in keys_to_purge)
    clean_key_row = safedb.query_one(
        """SELECT gk.key_id FROM group_keys gk
           LEFT JOIN keys_to_purge ktp ON gk.key_id = ktp.key_id AND ktp.recorded_by = ?
           WHERE gk.recorded_by = ? AND ktp.key_id IS NULL
           ORDER BY gk.created_at DESC
           LIMIT 1""",
        (peer_id, peer_id)
    )

    if clean_key_row:
        key_id = clean_key_row['key_id']
        log.info(f"group_key.get_or_create_clean_key() found existing clean key {key_id[:20]}...")
        return key_id

    # No clean key exists, create a new one
    log.info(f"group_key.get_or_create_clean_key() no clean key found, creating new one")
    key_id = create(peer_id, t_ms, db)
    log.info(f"group_key.get_or_create_clean_key() created new key {key_id[:20]}...")
    return key_id


def list(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all group keys with status and message counts.

    Returns keys in two states:
    - active: in group_keys, not in keys_to_purge
    - pending_purge: in group_keys AND in keys_to_purge

    Once purged, keys are gone (not shown).

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of dicts with: key_id, status, message_count, created_at
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    keys = safedb.query(
        """SELECT gk.key_id, gk.created_at,
                  CASE WHEN ktp.key_id IS NOT NULL THEN 'pending_purge' ELSE 'active' END as status,
                  (SELECT COUNT(*) FROM messages m
                   WHERE m.key_id = gk.key_id AND m.recorded_by = ?) as message_count
           FROM group_keys gk
           LEFT JOIN keys_to_purge ktp ON gk.key_id = ktp.key_id AND ktp.recorded_by = gk.recorded_by
           WHERE gk.recorded_by = ?
           ORDER BY gk.created_at DESC""",
        (peer_id, peer_id)
    )

    return [row for row in keys]


def rotate_for_removal(group_id: str, peer_id: str, peer_shared_id: str,
                       t_ms: int, removed_user_id: str, db: Any) -> str:
    """Rotate a group's encryption key when a member is removed.

    Creates a new key and shares it with all remaining members (excluding removed user).

    Args:
        group_id: Group whose key should be rotated
        peer_id: Local peer ID performing the rotation
        peer_shared_id: Public peer ID performing the rotation
        t_ms: Base timestamp for key creation
        removed_user_id: User ID being removed (to exclude from sharing)
        db: Database connection

    Returns:
        new_key_id: The newly created group key ID

    Raises:
        ValueError: If group not found
    """
    from events.group import group_key_shared

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Verify group exists
    group_row = safedb.query_one(
        "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ? LIMIT 1",
        (group_id, peer_id)
    )
    if not group_row:
        raise ValueError(f"Group {group_id} not found")

    log.info(f"group_key.rotate_for_removal() rotating key for group={group_id[:20]}..., removed_user={removed_user_id[:20]}...")

    # Create new group key
    new_key_id = create(peer_id=peer_id, t_ms=t_ms, db=db)
    log.info(f"group_key.rotate_for_removal() created new key_id={new_key_id[:20]}...")

    # Update group with new key_id
    safedb.execute(
        "UPDATE groups SET key_id = ? WHERE group_id = ? AND recorded_by = ?",
        (new_key_id, group_id, peer_id)
    )
    log.info(f"group_key.rotate_for_removal() updated group {group_id[:20]}... with new key")

    # Share new key with all remaining members (excluding removed user)
    group_key_shared.share_key_with_group_members(
        key_id=new_key_id,
        group_id=group_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,  # No offset needed - DAG deps handle ordering
        db=db,
        exclude_user_id=removed_user_id
    )

    log.info(f"group_key.rotate_for_removal() completed rotation for group {group_id[:20]}..., new_key={new_key_id[:20]}...")
    return new_key_id


def rotate_for_split_brain(
    group_id: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any
) -> str:
    """Rotate key to handle split-brain scenario.

    Creates a new key that excludes ALL currently removed users.
    Unlike rotate_for_removal(), this doesn't target a specific user.

    Args:
        group_id: Group whose key should be rotated
        peer_id: Local peer ID performing the rotation
        peer_shared_id: Public peer ID performing the rotation
        t_ms: Timestamp for key creation
        db: Database connection

    Returns:
        new_key_id: The newly created group key ID

    Raises:
        ValueError: If group not found
    """
    from events.group import group_key_shared

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Verify group exists
    group_row = safedb.query_one(
        "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ? LIMIT 1",
        (group_id, peer_id)
    )
    if not group_row:
        raise ValueError(f"Group {group_id} not found")

    log.info(f"group_key.rotate_for_split_brain() rotating key for group={group_id[:20]}...")

    # Create new key
    new_key_id = create(peer_id=peer_id, t_ms=t_ms, db=db)
    log.info(f"group_key.rotate_for_split_brain() created new key_id={new_key_id[:20]}...")

    # Update group's key_id
    safedb.execute(
        "UPDATE groups SET key_id = ? WHERE group_id = ? AND recorded_by = ?",
        (new_key_id, group_id, peer_id)
    )

    # Get all removed user_ids to exclude
    removed_users = safedb.query(
        "SELECT user_id FROM removed_users WHERE recorded_by = ?",
        (peer_id,)
    )
    removed_user_ids = {r['user_id'] for r in removed_users}

    # Get all current members (excluding removed users and self)
    if removed_user_ids:
        placeholders = ','.join('?' * len(removed_user_ids))
        all_members = safedb.query(
            f"""SELECT DISTINCT ps.peer_shared_id
               FROM group_members gm
               JOIN peers_shared ps ON gm.user_id = ps.user_id AND ps.recorded_by = gm.recorded_by
               WHERE gm.group_id = ? AND ps.peer_shared_id != ?
                 AND gm.user_id NOT IN ({placeholders}) AND gm.recorded_by = ?""",
            (group_id, peer_shared_id, *removed_user_ids, peer_id)
        )
    else:
        all_members = safedb.query(
            """SELECT DISTINCT ps.peer_shared_id
               FROM group_members gm
               JOIN peers_shared ps ON gm.user_id = ps.user_id AND ps.recorded_by = gm.recorded_by
               WHERE gm.group_id = ? AND ps.peer_shared_id != ? AND gm.recorded_by = ?""",
            (group_id, peer_shared_id, peer_id)
        )

    member_ids = [m['peer_shared_id'] for m in all_members]

    # Share with all remaining members
    if member_ids:
        group_key_shared.share_key_with_specific_members(
            key_id=new_key_id,
            member_peer_shared_ids=member_ids,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms + 1,
            db=db,
            group_id=group_id,
        )

    log.info(f"group_key.rotate_for_split_brain() completed rotation for group {group_id[:20]}..., new_key={new_key_id[:20]}..., shared with {len(member_ids)} members")
    return new_key_id
