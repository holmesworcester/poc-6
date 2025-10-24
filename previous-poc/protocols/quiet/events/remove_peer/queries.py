"""
Query and authorization helpers for remove_peer events.
"""
import sqlite3
from typing import Set, Optional


def get_removed_peers(db: sqlite3.Connection) -> Set[str]:
    """Get all removed peer IDs from database."""
    cursor = db.execute("SELECT peer_id FROM removed_peers")
    return {row['peer_id'] for row in cursor.fetchall()}


def is_peer_removed(peer_id: str, db: sqlite3.Connection) -> bool:
    """Check if a peer has been removed."""
    cursor = db.execute(
        "SELECT 1 FROM removed_peers WHERE peer_id = ? LIMIT 1",
        (peer_id,)
    )
    return cursor.fetchone() is not None


def get_removal_info(peer_id: str, db: sqlite3.Connection) -> Optional[dict]:
    """Get removal information for a peer."""
    cursor = db.execute(
        "SELECT peer_id, removed_at, removed_by, reason FROM removed_peers WHERE peer_id = ?",
        (peer_id,)
    )
    row = cursor.fetchone()
    if row:
        return dict(row)
    return None


def can_remove_peer(signer_peer_id: str, target_peer_id: str, network_id: str, db: sqlite3.Connection) -> bool:
    """
    Check if a peer has permission to remove another peer.

    Authorization rules:
    1. A peer can remove itself
    2. A peer can remove its linked peers (peers for the same user)
    3. An admin can remove any peer
    """
    # Rule 1: Self-removal
    if signer_peer_id == target_peer_id:
        return True

    # Find signer's user
    cursor = db.execute(
        "SELECT user_id FROM users WHERE peer_id = ? LIMIT 1",
        (signer_peer_id,)
    )
    signer_row = cursor.fetchone()
    if not signer_row:
        # Signer peer not found, cannot remove
        return False

    signer_user_id = signer_row['user_id']

    # Find target's user
    cursor = db.execute(
        "SELECT user_id FROM users WHERE peer_id = ? LIMIT 1",
        (target_peer_id,)
    )
    target_row = cursor.fetchone()
    if not target_row:
        # Target peer not found, cannot remove
        return False

    target_user_id = target_row['user_id']

    # Rule 2: Linked peer removal (same user)
    if signer_user_id == target_user_id:
        return True

    # Rule 3: Admin can remove any peer
    if is_user_admin(signer_user_id, network_id, db):
        return True

    return False


def is_user_admin(user_id: str, network_id: str, db: sqlite3.Connection) -> bool:
    """
    Check if a user is an admin in a network.

    A user is admin if they are a member of the network's admin group.
    """
    # Look for a group that is marked as the admin group for this network
    cursor = db.execute("""
        SELECT group_id FROM groups
        WHERE network_id = ? AND name LIKE '%admin%'
        LIMIT 1
    """, (network_id,))
    admin_group = cursor.fetchone()

    if not admin_group:
        return False

    admin_group_id = admin_group['group_id']

    # Check if user is a member of the admin group
    cursor = db.execute("""
        SELECT 1 FROM group_members
        WHERE group_id = ? AND user_id = ?
        LIMIT 1
    """, (admin_group_id, user_id))

    return cursor.fetchone() is not None
