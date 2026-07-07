"""Admin action chain helpers - tracks and validates admin-gated events in a DAG.

Admin-gated events (invite, admin, group_member, channel, user_removed) form a DAG via
the 'prior' field:
- prior[0] = previous action from same device (maintains per-device chain)
- prior[1] = optional merge reference (enables combining chains)

This module provides helpers to:
1. Get the latest admin action for a peer (for building chains)
2. Record admin actions after projection (for DAG traversal)
3. Validate actions against removal (DAG ancestry check)
"""

from typing import Any
import logging
from core.db import create_safe_db

log = logging.getLogger(__name__)


def get_latest_for_peer(peer_shared_id: str, recorded_by: str, db: Any) -> str | None:
    """Get the latest admin action ID for a peer.

    Used when creating new admin-gated events to build the prior[0] chain link.

    Args:
        peer_shared_id: The peer whose latest action to find
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        The action_id of the latest admin action, or None if no prior actions
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    row = safedb.query_one(
        """SELECT action_id FROM admin_actions
           WHERE peer_shared_id = ? AND recorded_by = ?
           ORDER BY created_at DESC LIMIT 1""",
        (peer_shared_id, recorded_by)
    )

    return row['action_id'] if row else None


def record_action(
    action_id: str,
    action_type: str,
    peer_shared_id: str,
    prior: list[str | None] | None,
    created_at: int,
    recorded_by: str,
    recorded_at: int,
    db: Any
) -> None:
    """Record an admin action in the admin_actions table.

    Called from projections after successful validation to build the DAG.

    Args:
        action_id: The event ID of this admin action
        action_type: One of 'invite', 'admin', 'group_member', 'channel', 'user_removed'
        peer_shared_id: The peer who created this action
        prior: The prior field from the event (up to 2 entries)
        created_at: Timestamp from the event
        recorded_by: Peer perspective
        recorded_at: When this peer recorded the event
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Extract prior[0] and prior[1] from the array
    prior_0 = prior[0] if prior and len(prior) > 0 else None
    prior_1 = prior[1] if prior and len(prior) > 1 else None

    safedb.execute(
        """INSERT OR IGNORE INTO admin_actions
           (action_id, action_type, peer_shared_id, prior_0, prior_1, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (action_id, action_type, peer_shared_id, prior_0, prior_1, created_at, recorded_by, recorded_at)
    )

    log.debug(f"admin_action.record_action() recorded {action_type} {action_id[:20]}... "
              f"for peer {peer_shared_id[:20]}... prior=[{prior_0 and prior_0[:20]}..., {prior_1 and prior_1[:20]}...]")


def is_dag_ancestor(ancestor_id: str | None, descendant_id: str | None, recorded_by: str, db: Any) -> bool:
    """Check if ancestor_id is reachable from descendant_id via prior links.

    Walks the DAG backwards from descendant following prior[0] and prior[1] links
    until ancestor is found or all paths exhausted.

    Args:
        ancestor_id: The ID to search for (potential ancestor)
        descendant_id: The ID to start walking from (potential descendant)
        recorded_by: Peer perspective for queries
        db: Database connection

    Returns:
        True if ancestor_id is reachable from descendant_id, False otherwise
    """
    if not ancestor_id or not descendant_id:
        return False

    if ancestor_id == descendant_id:
        return True

    safedb = create_safe_db(db, recorded_by=recorded_by)
    visited = set()
    queue = [descendant_id]

    while queue:
        current = queue.pop(0)
        if current in visited or not current:
            continue
        if current == ancestor_id:
            return True
        visited.add(current)

        # Get prior links for current action
        row = safedb.query_one(
            "SELECT prior_0, prior_1 FROM admin_actions WHERE action_id = ? AND recorded_by = ?",
            (current, recorded_by)
        )
        if row:
            if row['prior_0']:
                queue.append(row['prior_0'])
            if row['prior_1']:
                queue.append(row['prior_1'])

    return False


def get_removal_for_user_of_peer(peer_shared_id: str, recorded_by: str, db: Any) -> str | None:
    """Get the removal event ID for the user associated with a peer.

    Args:
        peer_shared_id: The peer to check
        recorded_by: Peer perspective
        db: Database connection

    Returns:
        The user_removed event ID if user is removed, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get user_id for this peer
    peer_row = safedb.query_one(
        "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
        (peer_shared_id, recorded_by)
    )
    if not peer_row or not peer_row['user_id']:
        return None

    user_id = peer_row['user_id']

    # Check if user is in removed_users
    # Note: user_removed events are tracked in admin_actions table
    # The removal_id is the action_id where action_type='user_removed' for this user
    removal_row = safedb.query_one(
        """SELECT action_id FROM admin_actions
           WHERE action_type = 'user_removed' AND recorded_by = ?
           AND action_id IN (
               SELECT action_id FROM admin_actions aa
               JOIN (SELECT user_id FROM removed_users WHERE user_id = ? AND recorded_by = ?) ru
               ON 1=1
           )
           LIMIT 1""",
        (recorded_by, user_id, recorded_by)
    )

    # Simpler: just check removed_users directly and find corresponding action
    if not removal_row:
        removed_row = safedb.query_one(
            "SELECT 1 FROM removed_users WHERE user_id = ? AND recorded_by = ?",
            (user_id, recorded_by)
        )
        if removed_row:
            # User is removed but we don't have the action_id from admin_actions
            # This means the removal predates the admin_action tracking
            # Return a sentinel value that will cause validation to fail
            log.warning(f"admin_action.get_removal_for_user_of_peer() user {user_id[:20]}... "
                       f"is removed but no admin_action entry found")
            return "REMOVED_NO_ACTION"  # Special sentinel

    return removal_row['action_id'] if removal_row else None


def is_valid_after_removal(action_id: str, signer_peer_id: str, recorded_by: str, db: Any) -> bool:
    """Check if an admin action from a potentially-removed user's peer is valid.

    The action is valid if:
    1. The user is not removed, OR
    2. The action_id is reachable from the removal event via prior links
       (meaning it was created before or at the point of removal)

    Args:
        action_id: The admin action to validate
        signer_peer_id: The peer who signed the action
        recorded_by: Peer perspective
        db: Database connection

    Returns:
        True if action is valid, False if it should be rejected
    """
    removal_id = get_removal_for_user_of_peer(signer_peer_id, recorded_by, db)

    if not removal_id:
        # User is not removed - action is valid
        return True

    if removal_id == "REMOVED_NO_ACTION":
        # User removed but no admin_action tracking - reject to be safe
        log.warning(f"admin_action.is_valid_after_removal() rejecting action {action_id[:20]}... "
                   f"from removed user with no action tracking")
        return False

    # Check DAG ancestry: is action_id reachable from removal_id?
    is_ancestor = is_dag_ancestor(action_id, removal_id, recorded_by, db)

    if is_ancestor:
        log.info(f"admin_action.is_valid_after_removal() action {action_id[:20]}... "
                f"is ancestor of removal - VALID")
    else:
        log.warning(f"admin_action.is_valid_after_removal() action {action_id[:20]}... "
                   f"is NOT ancestor of removal - INVALID (post-removal action)")

    return is_ancestor


def build_prior(peer_shared_id: str, recorded_by: str, db: Any, merge_ref: str | None = None) -> list[str]:
    """Build the prior array for a new admin-gated event.

    Args:
        peer_shared_id: The peer creating the action
        recorded_by: Peer perspective (usually same as local peer_id)
        db: Database connection
        merge_ref: Optional reference to merge (another action or removal)

    Returns:
        prior array with up to 2 entries: [own_chain_link, merge_ref]
        Empty entries are omitted to minimize event size.
    """
    prior = []

    # Get latest action from this peer for chain link
    latest = get_latest_for_peer(peer_shared_id, recorded_by, db)
    if latest:
        prior.append(latest)

    # Add merge reference if provided and different from chain link
    if merge_ref and merge_ref != latest:
        if not prior:
            prior.append(None)  # Need placeholder for prior[0] if empty
        prior.append(merge_ref)

    return prior
