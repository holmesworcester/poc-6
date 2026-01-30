"""Sender key management for sender-initiated key distribution.

In the sender key model:
- Each sender creates their own symmetric key per group
- Sender distributes their key to recipients via TreeKEM copath
- Messages are encrypted with the sender's key
- Recipients decrypt using the sender's key they received

This provides:
- O(log n) key distribution per sender (vs O(n) for group keys)
- Forward secrecy on sender removal
- Reduced trust requirements (sender controls their own keys)

Key security property:
- Keys are bound to removal_epochs - a key created after a removal references
  that removal epoch, so key requests can only be fulfilled by peers who have
  also seen that removal (and thus won't share with removed users).
"""

from typing import Any
import logging
from core import crypto
from core import treekem
from core.db import create_safe_db
from events.group import secret, secret_shared, key_announce, treekem_pubkey
from events.identity import removal_epoch

log = logging.getLogger(__name__)


def pick_or_create_key(
    group_id: str,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> dict[str, Any]:
    """Get or create a sender key for a group.

    If the sender already has a valid key for this group, returns it.
    Otherwise, creates a new key, announces it, and distributes via TreeKEM.

    Args:
        group_id: Group to get/create key for
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (sender identity)
        t_ms: Timestamp
        db: Database connection

    Returns:
        Key dict for crypto operations with 'id' (bytes), 'key' (bytes), 'type' = 'symmetric'
    """
    log.info(f"sender_key.pick_or_create_key() group={group_id[:20]}..., sender={peer_shared_id[:20]}...")

    # Look for existing sender key for this group
    existing_key_id = get_sender_key_for_group(group_id, peer_shared_id, peer_id, db)

    if existing_key_id:
        # Get the key data
        key_data = secret.get_key(existing_key_id, peer_id, db)
        if key_data:
            log.info(f"sender_key.pick_or_create_key() found existing key={existing_key_id[:20]}...")
            return key_data

    # No existing key, create a new one
    log.info(f"sender_key.pick_or_create_key() creating new key for group={group_id[:20]}...")

    # Get current removal epoch - keys are bound to the removal state at creation time
    # This ensures key requests can only be fulfilled by peers who know about the same removals
    current_epoch_id = removal_epoch.get_current_epoch(peer_id, db)

    # Create the secret (this also creates key_announce with the removal_epoch_id)
    secret_id = secret.create(
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        group_id=group_id,
        removal_epoch_id=current_epoch_id,
        t_ms=t_ms,
        db=db,
    )

    # Distribute the key via TreeKEM copath
    distribute_key_to_group(
        secret_id=secret_id,
        group_id=group_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        removal_epoch_id=current_epoch_id,
        t_ms=t_ms + 1,
        db=db,
    )

    # Return the key data
    key_data = secret.get_key(secret_id, peer_id, db)
    log.info(f"sender_key.pick_or_create_key() created and distributed key={secret_id[:20]}...")
    return key_data


def get_sender_key_for_group(
    group_id: str,
    sender_peer_shared_id: str,
    recorded_by: str,
    db: Any,
) -> str | None:
    """Get the sender's current key for a group.

    Args:
        group_id: Group ID
        sender_peer_shared_id: Sender's peer_shared_id
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        secret_id if found, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Look up the most recent key announcement from this sender for this group
    # that we have the actual secret for
    row = safedb.query_one(
        """SELECT ka.key_id FROM key_announces ka
           INNER JOIN secrets s ON s.secret_id = ka.key_id AND s.recorded_by = ka.recorded_by
           WHERE ka.group_id = ? AND ka.signed_by = ? AND ka.recorded_by = ?
           ORDER BY ka.created_at DESC
           LIMIT 1""",
        (group_id, sender_peer_shared_id, recorded_by)
    )

    return row['key_id'] if row else None


def get_key_for_decryption(
    key_id: str,
    recorded_by: str,
    db: Any,
) -> dict[str, Any] | None:
    """Get a key for decryption (lookup by key_id).

    This is used by recipients to decrypt messages from senders.

    Args:
        key_id: The key ID to look up
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Key dict or None if not found
    """
    try:
        return secret.get_key(key_id, recorded_by, db)
    except ValueError:
        return None


def distribute_key_to_group(
    secret_id: str,
    group_id: str,
    peer_id: str,
    peer_shared_id: str,
    removal_epoch_id: str | None,
    t_ms: int,
    db: Any,
) -> list[str]:
    """Distribute a key to all group members via TreeKEM copath.

    Uses the TreeKEM optimization to achieve O(log n) key distribution.

    Args:
        secret_id: Secret to distribute
        group_id: Group to distribute to
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (sender)
        removal_epoch_id: Current removal epoch (for forward secrecy)
        t_ms: Timestamp
        db: Database connection

    Returns:
        List of secret_shared event IDs created
    """
    log.info(f"sender_key.distribute_key_to_group() secret={secret_id[:20]}..., group={group_id[:20]}..., epoch={removal_epoch_id}")

    # Get all group members' peer_shared_ids
    member_peer_ids = get_group_member_peer_ids(group_id, peer_id, db)

    if not member_peer_ids:
        log.warning(f"sender_key.distribute_key_to_group() no members found for group")
        return []

    # Compute copath for sender
    copath = treekem.compute_copath_for_sender(peer_shared_id, member_peer_ids)

    if not copath:
        log.info(f"sender_key.distribute_key_to_group() empty copath (only sender in group)")
        return []

    # Get pubkeys for copath positions
    copath_pubkey_ids = get_copath_pubkeys(copath, peer_id, db)

    if not copath_pubkey_ids:
        # Fallback to leaf distribution (O(n)) if no copath pubkeys available
        log.info(f"sender_key.distribute_key_to_group() falling back to leaf distribution")
        return distribute_to_leaves(
            secret_id=secret_id,
            member_peer_ids=[m for m in member_peer_ids if m != peer_shared_id],
            peer_id=peer_id,
            peer_shared_id=peer_shared_id,
            removal_epoch_id=removal_epoch_id,
            t_ms=t_ms,
            db=db,
        )

    # Distribute to copath pubkeys (O(log n))
    log.info(f"sender_key.distribute_key_to_group() distributing to {len(copath_pubkey_ids)} copath nodes")
    return secret_shared.share_secret_with_pubkeys(
        secret_id=secret_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        recipient_pubkey_ids=copath_pubkey_ids,
        removal_epoch_id=removal_epoch_id,
        t_ms=t_ms,
        db=db,
    )


def distribute_to_leaves(
    secret_id: str,
    member_peer_ids: list[str],
    peer_id: str,
    peer_shared_id: str,
    removal_epoch_id: str | None,
    t_ms: int,
    db: Any,
) -> list[str]:
    """Fallback distribution to leaf pubkeys (O(n)).

    Used when copath pubkeys aren't available.

    Args:
        secret_id: Secret to distribute
        member_peer_ids: List of recipient peer_shared_ids
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (sender)
        removal_epoch_id: Current removal epoch (for forward secrecy)
        t_ms: Timestamp
        db: Database connection

    Returns:
        List of secret_shared event IDs created
    """
    from events.group import pubkey_shared

    log.info(f"sender_key.distribute_to_leaves() distributing to {len(member_peer_ids)} members, epoch={removal_epoch_id}")

    shared_ids = []
    for i, recipient_peer_id in enumerate(member_peer_ids):
        # Get recipient's pubkey from pubkeys_shared table
        recipient_pk = pubkey_shared.get_pubkey_for_peer(recipient_peer_id, peer_id, db)
        if not recipient_pk:
            log.warning(f"sender_key.distribute_to_leaves() no pubkey for {recipient_peer_id[:20]}...")
            continue

        recipient_pubkey_id = crypto.b64encode(recipient_pk['id'])

        try:
            shared_id = secret_shared.create_to_pubkey(
                secret_id=secret_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_pubkey_id=recipient_pubkey_id,
                removal_epoch_id=removal_epoch_id,
                t_ms=t_ms + i,
                db=db,
            )
            shared_ids.append(shared_id)
        except Exception as e:
            log.warning(f"sender_key.distribute_to_leaves() failed for {recipient_peer_id[:20]}...: {e}")

    return shared_ids


def get_group_member_peer_ids(group_id: str, recorded_by: str, db: Any) -> list[str]:
    """Get all peer_shared_ids for members of a group.

    Args:
        group_id: Group ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of peer_shared_ids
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get group members and their peer_shared_ids
    rows = safedb.query(
        """SELECT DISTINCT ps.peer_shared_id
           FROM group_members gm
           INNER JOIN peers_shared ps ON ps.user_id = gm.user_id AND ps.recorded_by = gm.recorded_by
           WHERE gm.group_id = ? AND gm.recorded_by = ?""",
        (group_id, recorded_by)
    )

    return [row['peer_shared_id'] for row in rows]


def get_copath_pubkeys(
    copath: list[tuple[int, bytes]],
    recorded_by: str,
    db: Any,
) -> list[str]:
    """Get treekem_pubkey IDs for copath positions.

    Args:
        copath: List of (depth, path_prefix) tuples
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of treekem_pubkey_ids (may be fewer than copath if some missing)
    """
    pubkey_ids = []

    for depth, path_prefix in copath:
        # Look for a treekem_pubkey at this position
        pk = treekem_pubkey.get_pubkey_at_position(depth, path_prefix, recorded_by, db)
        if pk:
            # get_pubkey_at_position returns {'id': bytes, 'public_key': bytes, 'type': 'asymmetric'}
            pubkey_ids.append(crypto.b64encode(pk['id']))

    return pubkey_ids


def get_all_keys_for_group(group_id: str, recorded_by: str, db: Any) -> list[str]:
    """Get all sender keys we know about for a group.

    This returns all keys that have been announced for the group AND that
    we have the actual secret for (in our secrets table).

    Args:
        group_id: Group ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of secret_ids (sender keys)
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get all announced keys for this group that we have locally
    rows = safedb.query(
        """SELECT DISTINCT ka.key_id FROM key_announces ka
           INNER JOIN secrets s ON s.secret_id = ka.key_id AND s.recorded_by = ka.recorded_by
           WHERE ka.group_id = ? AND ka.recorded_by = ?""",
        (group_id, recorded_by)
    )

    return [row['key_id'] for row in rows]


def share_all_keys_to_pubkey(
    group_id: str,
    recipient_pubkey_id: str,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> list[str]:
    """Share all known sender keys for a group to a specific pubkey.

    This is used when creating invites - the invitee needs all sender keys
    to read past messages from all senders.

    Args:
        group_id: Group ID to share keys for
        recipient_pubkey_id: Pubkey ID to encrypt keys to (e.g., invite pubkey)
        peer_id: Local peer ID
        peer_shared_id: Public peer ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        List of secret_shared_ids created
    """
    log.info(f"sender_key.share_all_keys_to_pubkey() group={group_id[:20]}..., recipient={recipient_pubkey_id[:20]}...")

    # Get all keys for this group
    key_ids = get_all_keys_for_group(group_id, peer_id, db)

    if not key_ids:
        log.info(f"sender_key.share_all_keys_to_pubkey() no keys found for group")
        return []

    log.info(f"sender_key.share_all_keys_to_pubkey() sharing {len(key_ids)} keys")

    shared_ids = []
    for i, key_id in enumerate(key_ids):
        try:
            shared_id = secret_shared.create_to_pubkey(
                secret_id=key_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_pubkey_id=recipient_pubkey_id,
                removal_epoch_id=None,
                t_ms=t_ms + i,
                db=db,
            )
            shared_ids.append(shared_id)
            log.info(f"sender_key.share_all_keys_to_pubkey() shared key {key_id[:20]}... -> {shared_id[:20]}...")
        except Exception as e:
            log.warning(f"sender_key.share_all_keys_to_pubkey() failed for {key_id[:20]}...: {e}")

    return shared_ids
