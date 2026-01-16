"""Group key event type (subjective symmetric keys for network/group content encryption)."""

# Registry metadata
EVENT_TYPE = 'group_key'
SHAREABLE = False  # Local-only - contains symmetric key material
EPHEMERAL = False
PROJECTION_TABLE = None

from typing import Any
import json
import logging
from core import crypto
from core import store
from core.db import create_safe_db
from core.projection_v2.types import ProjectorResult, WriteOp

EVENT_SPEC = {
    'encrypted': False,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}

log = logging.getLogger(__name__)


def create(peer_id: str, t_ms: int, db: Any) -> str:
    """Create a group key for network content encryption, owned by peer_id."""
    log.info(f"group_key.create() creating new group key for peer_id={peer_id}, t_ms={t_ms}")

    # Generate symmetric key
    key = crypto.generate_secret()

    # Create DETERMINISTIC event blob - only type and key
    # This ensures same key material = same key_id on all peers
    event_data = {
        'type': 'group_key',
        'key': crypto.b64encode(key)
    }

    # Use sort_keys=True for canonical ordering
    blob = json.dumps(event_data, sort_keys=True).encode()

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
    event_data = {
        'type': 'group_key',
        'key': crypto.b64encode(key_material)
    }

    # Use sort_keys=True for canonical ordering
    blob = json.dumps(event_data, sort_keys=True).encode()
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


def project(key_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project group key event into group_keys table.

    Args:
        key_id: The group_key event ID
        recorded_by: Peer projecting this event
        recorded_at: Timestamp to use for created_at (since blob has no timestamp)
        db: Database connection

    Returns:
        key_id on success, None on failure
    """
    log.debug(f"group_key.project() projecting key_id={key_id}, seen_by={recorded_by}")

    # Get blob from store
    blob = store.get(key_id, db)
    if not blob:
        log.warning(f"group_key.project() blob not found for key_id={key_id}")
        return None

    # Parse JSON
    event_data = crypto.parse_json(blob)

    # Insert into group_keys table (subjective)
    # Note: created_at comes from recorded_at, NOT the blob (deterministic blobs have no timestamp)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    log.debug(f"group_key.project() inserting key_id={key_id} into group_keys table")
    safedb.execute(
        """INSERT OR IGNORE INTO group_keys (key_id, key, created_at, recorded_by)
           VALUES (?, ?, ?, ?)""",
        (
            key_id,
            crypto.b64decode(event_data['key']),
            recorded_at,  # Use recorded_at since blob has no timestamp
            recorded_by
        )
    )

    log.info(f"group_key.project() projected key_id={key_id} into group_keys table")
    return key_id


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
