"""TreeKEM re-update logic for superseded members to converge on winning state.

In DH-based TreeKEM, when multiple members update concurrently (same base_update_id),
only one wins (lowest treekem_update_id). Superseded members must re-update based on
the winner's state to converge on the same root secret.

This module provides:
- check_and_reupdate(): Check if our update was superseded and create new one
- reupdate_all_peers(): Run re-update check for all local peers

The re-update ensures:
1. Superseded member sees winner's pubkeys at sibling positions
2. Superseded member computes DH(my_secret, winner's_pubkey)
3. Both winner and superseded member derive the SAME parent secret
4. Everyone converges to the same root → O(1) broadcast works!
"""

from typing import Any
import logging
from core import crypto
from core.db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def check_and_reupdate(
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str | None:
    """Check if our latest update was superseded and re-update if needed.

    Args:
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (signer identity)
        t_ms: Current timestamp
        db: Database connection

    Returns:
        New treekem_update_id if re-update created, None otherwise
    """
    from events.group import treekem_update
    from events.identity import removal_epoch

    # 1. Get our latest update
    my_update = treekem_update.get_latest_update_by_author(peer_shared_id, peer_id, db)
    if not my_update:
        return None  # No updates from us yet

    # 2. Check if it's the winner for its base
    winner = treekem_update.get_winning_update(
        my_update['removal_epoch_id'],
        my_update['base_update_id'],
        peer_id,
        db
    )

    if not winner:
        return None  # No concurrent updates

    if winner['treekem_update_id'] == my_update['treekem_update_id']:
        return None  # We won, no re-update needed

    # 3. Our update was superseded - create new update based on winner
    log.info(f"treekem_reupdate.check_and_reupdate() peer={peer_shared_id[:20]}... superseded by {winner['treekem_update_id'][:20]}...")

    # Get active members for resolution
    from events.group import sender_key
    current_epoch_id = removal_epoch.get_current_epoch(peer_id, db)

    # For re-update, we need to know the active group members
    # This is a simplified version - in practice should get from group context
    active_members = [peer_shared_id]

    # Create new update based on winner
    result = treekem_update.create_update_path_dh(
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        removal_epoch_id=my_update['removal_epoch_id'],
        base_update_id=winner['treekem_update_id'],  # Build on winner!
        active_members=active_members,
        t_ms=t_ms,
        db=db,
    )

    log.info(f"treekem_reupdate.check_and_reupdate() created re-update {result['treekem_update_id'][:20]}...")
    return result['treekem_update_id']


def reupdate_all_peers(t_ms: int, db: Any) -> dict[str, Any]:
    """Re-update all local peers that have superseded updates.

    Called by TreeKEMReUpdateJob to converge after concurrent updates.

    Args:
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Dict with stats about re-updates created
    """
    unsafedb = create_unsafe_db(db)
    peers = unsafedb.query("SELECT peer_id FROM local_peers")

    reupdates_created = 0
    for peer_row in peers:
        peer_id = peer_row['peer_id']

        try:
            # Get peer_shared_id
            safedb = create_safe_db(db, recorded_by=peer_id)
            peer_self = safedb.query_one(
                "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
                (peer_id, peer_id)
            )
            if not peer_self:
                continue

            peer_shared_id = peer_self['peer_shared_id']

            # Check and re-update if needed
            new_update_id = check_and_reupdate(
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                t_ms=t_ms,
                db=db,
            )

            if new_update_id:
                reupdates_created += 1

        except Exception as e:
            log.warning(f"reupdate_all_peers: failed for peer {peer_id[:20]}...: {e}")

    return {'reupdates_created': reupdates_created}


def is_superseded(
    treekem_update_id: str,
    recorded_by: str,
    db: Any,
) -> bool:
    """Check if an update was superseded (lost to a concurrent update).

    Args:
        treekem_update_id: The update to check
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        True if this update was superseded by another
    """
    from events.group import treekem_update

    update = treekem_update.get_update(treekem_update_id, recorded_by, db)
    if not update:
        return False

    winner = treekem_update.get_winning_update(
        update['removal_epoch_id'],
        update['base_update_id'],
        recorded_by,
        db
    )

    return winner is not None and winner['treekem_update_id'] != treekem_update_id


def get_superseded_updates(
    peer_shared_id: str,
    recorded_by: str,
    db: Any,
) -> list[dict[str, Any]]:
    """Get all superseded updates by a specific peer.

    Args:
        peer_shared_id: Author's peer_shared_id
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of superseded update records
    """
    from events.group import treekem_update

    safedb = create_safe_db(db, recorded_by=recorded_by)
    all_updates = safedb.query(
        """SELECT treekem_update_id, author_peer_id, leaf_pubkey_shared_id,
                  removal_epoch_id, base_update_id, created_at
           FROM treekem_updates
           WHERE author_peer_id = ? AND recorded_by = ?""",
        (peer_shared_id, recorded_by)
    )

    superseded = []
    for update in all_updates:
        if is_superseded(update['treekem_update_id'], recorded_by, db):
            superseded.append(update)

    return superseded
