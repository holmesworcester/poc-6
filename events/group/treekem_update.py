"""TreeKEM update orchestration module.

Handles proactive updates of TreeKEM pubkeys on a cadence.
This ensures peers always have fresh pubkeys published for O(log n) distribution.
"""

import logging
from typing import Any
from core import treekem
from core.db import create_safe_db, create_unsafe_db
from events.group import treekem_pubkey_shared

log = logging.getLogger(__name__)


def needs_update(
    group_id: str,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> bool:
    """Check if peer needs to publish TreeKEM updates for a group.

    Returns True if any pubkey on the path is missing or near expiry.

    Args:
        group_id: The group
        peer_id: Local peer ID
        peer_shared_id: Public peer ID
        t_ms: Current timestamp
        db: Database connection

    Returns:
        True if updates are needed
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get peer's path to root
    path = treekem.path_to_root(peer_shared_id)

    # Check if any node on path is missing or near expiry
    refresh_threshold = t_ms + treekem.TREEKEM_REFRESH_THRESHOLD_MS

    for depth, path_prefix in path:
        # Check if we have a valid pubkey for this node
        row = safedb.query_one(
            """SELECT ttl_ms FROM treekem_pubkeys
               WHERE group_id = ? AND depth = ? AND path_prefix = ? AND recorded_by = ?
               ORDER BY created_at DESC
               LIMIT 1""",
            (group_id, depth, path_prefix, peer_id)
        )

        if not row or row['ttl_ms'] < refresh_threshold:
            return True

    return False


def update_for_group(
    group_id: str,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> list[str]:
    """Publish TreeKEM updates for a group.

    Creates new pubkeys and shares them for all nodes on the path.

    Args:
        group_id: The group
        peer_id: Local peer ID
        peer_shared_id: Public peer ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        List of pubkey_shared_ids created
    """
    log.info(f"treekem_update.update_for_group() group={group_id[:20]}..., peer={peer_id[:20]}...")

    # Share all path pubkeys
    shared_ids = treekem_pubkey_shared.share_path_pubkeys(
        group_id=group_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,
        db=db,
    )

    log.info(f"treekem_update.update_for_group() shared {len(shared_ids)} pubkeys")
    return shared_ids


def update_for_all_groups(
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> dict[str, Any]:
    """Update TreeKEM pubkeys for all groups the peer is a member of.

    Args:
        peer_id: Local peer ID
        peer_shared_id: Public peer ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        Dict with stats: groups_processed, groups_updated, pubkeys_shared
    """
    from events.identity import network_settings

    log.info(f"treekem_update.update_for_all_groups() peer={peer_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=peer_id)

    stats = {
        'groups_processed': 0,
        'groups_updated': 0,
        'pubkeys_shared': 0,
    }

    # Get all groups this peer is a member of
    # (Groups are essentially networks in this codebase)
    network_rows = safedb.query(
        "SELECT DISTINCT network_id FROM networks WHERE recorded_by = ?",
        (peer_id,)
    )

    for network_row in network_rows:
        network_id = network_row['network_id']
        stats['groups_processed'] += 1

        # Check if TreeKEM is enabled for this network
        if not is_treekem_enabled(network_id, peer_id, db):
            continue

        # Check if update is needed
        if not needs_update(network_id, peer_id, peer_shared_id, t_ms, db):
            continue

        # Perform update
        shared_ids = update_for_group(
            group_id=network_id,
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            t_ms=t_ms,
            db=db,
        )

        stats['groups_updated'] += 1
        stats['pubkeys_shared'] += len(shared_ids)

    log.info(f"treekem_update.update_for_all_groups() complete: {stats}")
    return stats


def is_treekem_enabled(network_id: str, peer_id: str, db: Any) -> bool:
    """Check if TreeKEM is enabled for a network.

    Args:
        network_id: The network/group ID
        peer_id: Local peer ID
        db: Database connection

    Returns:
        True if TreeKEM is enabled
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Check network_settings for treekem_enabled flag
    row = safedb.query_one(
        """SELECT treekem_enabled FROM network_settings
           WHERE network_id = ? AND recorded_by = ?
           ORDER BY created_at DESC
           LIMIT 1""",
        (network_id, peer_id)
    )

    if not row:
        return False

    return row.get('treekem_enabled', False)
