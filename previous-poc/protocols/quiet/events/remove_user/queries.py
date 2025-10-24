"""
Query and authorization helpers for remove_user events.
"""
import sqlite3
from typing import Set, Optional


def get_removed_users(db: sqlite3.Connection) -> Set[str]:
    """Get all removed user IDs from database."""
    cursor = db.execute("SELECT user_id FROM removed_users")
    return {row['user_id'] for row in cursor.fetchall()}


def is_user_removed(user_id: str, db: sqlite3.Connection) -> bool:
    """Check if a user has been removed."""
    cursor = db.execute(
        "SELECT 1 FROM removed_users WHERE user_id = ? LIMIT 1",
        (user_id,)
    )
    return cursor.fetchone() is not None


def get_removal_info(user_id: str, db: sqlite3.Connection) -> Optional[dict]:
    """Get removal information for a user."""
    cursor = db.execute(
        "SELECT user_id, removed_at, removed_by, reason FROM removed_users WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    if row:
        return dict(row)
    return None


def get_peers_for_user(user_id: str, db: sqlite3.Connection) -> Set[str]:
    """Get all peers for a user."""
    cursor = db.execute(
        "SELECT peer_id FROM users WHERE user_id = ?",
        (user_id,)
    )
    return {row['peer_id'] for row in cursor.fetchall()}


def can_remove_user(signer_peer_id: str, target_user_id: str, network_id: str, db: sqlite3.Connection) -> bool:
    """
    Check if a peer has permission to remove a user.

    Authorization rules:
    1. A user (via any linked peer) can remove themselves
    2. An admin can remove any user, including other admins
    """
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

    # Rule 1: User removing self (via any linked peer)
    if signer_user_id == target_user_id:
        return True

    # Rule 2: Admin can remove any user
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
