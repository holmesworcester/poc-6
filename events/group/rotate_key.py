"""Group key rotation helper for member removal."""
from typing import Any
import logging
from events.group import group_key, group_key_shared
from db import create_safe_db

log = logging.getLogger(__name__)


def rotate_group_key(group_id: str, peer_id: str, peer_shared_id: str,
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
        ValueError: If group not found or cannot be rotated
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get current group info
    group_row = safedb.query_one(
        "SELECT key_id FROM groups WHERE group_id = ? AND recorded_by = ? LIMIT 1",
        (group_id, peer_id)
    )
    if not group_row:
        raise ValueError(f"Group {group_id} not found")

    log.info(f"key_rotation.rotate_group_key() rotating key for group={group_id}, removed_user={removed_user_id[:20]}...")

    # Create new group key
    new_key_id = group_key.create(peer_id=peer_id, t_ms=t_ms, db=db)
    log.info(f"key_rotation.rotate_group_key() created new key_id={new_key_id[:20]}...")

    # Update group with new key_id
    safedb.execute(
        "UPDATE groups SET key_id = ? WHERE group_id = ? AND recorded_by = ?",
        (new_key_id, group_id, peer_id)
    )
    log.info(f"key_rotation.rotate_group_key() updated group {group_id} with new key")

    # Share new key with all remaining members (excluding removed user)
    # Get members from group_members table
    members = safedb.query(
        """SELECT DISTINCT u.peer_id, gm.user_id
           FROM group_members gm
           JOIN users u ON gm.user_id = u.user_id AND u.recorded_by = gm.recorded_by
           WHERE gm.group_id = ? AND gm.user_id != ? AND gm.recorded_by = ?""",
        (group_id, removed_user_id, peer_id)
    )

    log.info(f"key_rotation.rotate_group_key() sharing new key with {len(members)} remaining members")

    # Share the new key with each remaining member
    share_timestamp = t_ms + 1
    for member in members:
        recipient_peer_id = member['peer_id']
        try:
            group_key_shared.create(
                key_id=new_key_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_peer_id=recipient_peer_id,
                t_ms=share_timestamp,
                db=db
            )
            log.info(f"key_rotation.rotate_group_key() shared new key with {recipient_peer_id[:20]}...")
            share_timestamp += 1
        except Exception as e:
            log.warning(f"key_rotation.rotate_group_key() failed to share key with {recipient_peer_id[:20]}...: {e}")
            # Continue sharing with other members even if one fails

    log.info(f"key_rotation.rotate_group_key() completed rotation for group {group_id}, new_key={new_key_id[:20]}...")
    return new_key_id
