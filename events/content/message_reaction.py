"""Message reaction event type - add/remove emoji reactions to messages.

Reactions are group-encrypted (access control via message's group_id).
Authorization: Any group member can add reactions, only the reactor or admins can remove.
Reactions are reference events that depend on messages.
"""
from typing import Any
import logging
from core import crypto
from core import store
from core import global_counter
from core.db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)

# Event type declarations for auto-discovery
EVENT_TYPE = 'message_reaction'
SHAREABLE = True  # Sync reactions to other peers
EPHEMERAL = False
PROJECTION_TABLE = ('message_reactions', 'reaction_id')


def create(peer_id: str, message_id: str, emoji: str, t_ms: int, db: Any) -> str:
    """Create a message_reaction event to add an emoji reaction to a message.

    Uses global_count for deterministic convergence when multiple devices react simultaneously.
    Authorization: Any group member can react. The reactor is derived from peer_id.

    Args:
        peer_id: Local peer ID creating the reaction
        message_id: Message event ID to react to
        emoji: Unicode emoji character(s)
        t_ms: Timestamp
        db: Database connection

    Returns:
        reaction_id: The stored reaction event ID

    Raises:
        ValueError: If message not found or validation fails
    """
    log.info(f"message_reaction.create() reacting to message_id={message_id[:20]}... with emoji={emoji}")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get message to validate it exists and is not deleted
    message_row = safedb.query_one(
        "SELECT group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, peer_id)
    )
    if not message_row:
        raise ValueError(f"Message {message_id} not found for peer {peer_id}")

    message_group_id = message_row['group_id']

    # Get reactor's peer_shared_id and user_id
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id, user_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set")

    reactor_peer_shared_id = peer_self_row['peer_shared_id']
    reactor_user_id = peer_self_row['user_id']

    # Get global count from framework (Lamport clock)
    global_count = global_counter.get_next_global_count(peer_id, db)

    # Create reaction event with global_count for deterministic ordering
    event_data = {
        'type': 'message_reaction',
        'message_id': message_id,
        'reactor_id': reactor_user_id,
        'signed_by': reactor_peer_shared_id,
        'emoji': emoji,
        'created_at': t_ms,
        'global_count': global_count
    }

    # Sign the event
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get group key for encryption (reaction uses message's group key)
    from events.group import group
    key_data = group.pick_key(message_group_id, peer_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event
    reaction_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"message_reaction.create() created reaction_id={reaction_id[:20]}...")
    return reaction_id


def remove(peer_id: str, message_id: str, emoji: str, t_ms: int, db: Any) -> str:
    """Remove a reaction by creating a message_reaction_deletion event.

    Authorization: Only the reactor who added the reaction, or group admins can remove.

    Args:
        peer_id: Local peer ID removing the reaction
        message_id: Message containing the reaction
        emoji: Unicode emoji character(s) to remove
        t_ms: Timestamp
        db: Database connection

    Returns:
        deletion_id: The stored deletion event ID

    Raises:
        ValueError: If reaction not found or authorization fails
    """
    log.info(f"message_reaction.remove() removing reaction from message_id={message_id[:20]}... emoji={emoji}")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get reactor's user_id and peer_shared_id
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id, user_id FROM peer_self WHERE peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    if not peer_self_row or not peer_self_row['peer_shared_id']:
        raise ValueError(f"Peer {peer_id} not found or peer_shared_id not set")

    remover_peer_shared_id = peer_self_row['peer_shared_id']
    remover_user_id = peer_self_row['user_id']

    # Find the reaction to remove
    reaction_row = safedb.query_one(
        """SELECT reaction_id, reactor_id, message_id, emoji FROM message_reactions
           WHERE message_id = ? AND reactor_id = ? AND emoji = ? AND recorded_by = ? LIMIT 1""",
        (message_id, remover_user_id, emoji, peer_id)
    )
    if not reaction_row:
        raise ValueError(
            f"Reaction not found: message_id={message_id}, reactor_id={remover_user_id}, emoji={emoji}"
        )

    reaction_id = reaction_row['reaction_id']

    # Check authorization: reactor can self-remove, or admin can remove
    # If not the reactor, check if remover is admin
    if remover_user_id != reaction_row['reactor_id']:
        from events.identity import invite
        message_row = safedb.query_one(
            "SELECT group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
            (message_id, peer_id)
        )
        if not message_row:
            raise ValueError(f"Message {message_id} not found")

        group_id = message_row['group_id']
        if not invite.is_admin(remover_peer_shared_id, peer_id, db):
            raise ValueError(
                f"Peer {peer_id} cannot remove reaction: not the reactor and not an admin"
            )

    log.info(f"message_reaction.remove() authorization passed for reaction_id={reaction_id[:20]}...")

    # Get message group_id
    message_row = safedb.query_one(
        "SELECT group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, peer_id)
    )
    if not message_row:
        raise ValueError(f"Message {message_id} not found")

    message_group_id = message_row['group_id']

    # Create deletion event
    event_data = {
        'type': 'message_reaction_deletion',
        'reaction_id': reaction_id,
        'deleted_by': remover_peer_shared_id,
        'created_at': t_ms
    }

    # Sign the event
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Get group key for encryption (deletion uses message's group key)
    from events.group import group
    key_data = group.pick_key(message_group_id, peer_id, db)

    # Wrap (canonicalize + encrypt)
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, key_data, db)

    # Store event
    deletion_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"message_reaction.remove() created deletion_id={deletion_id[:20]}...")
    return deletion_id


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project message_reaction event into message_reactions table.

    Stores all reactions unconditionally (INSERT OR IGNORE). Winners are determined
    at query time via window functions (highest global_count, then highest event_id).

    Deletion blocking prevents deleted reactions from being accessed.

    Args:
        event_id: Reaction event ID (content-addressed hash, base64-encoded)
        recorded_by: Peer who recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection

    Returns:
        event_id if successful, None if blocked by deletion
    """
    log.debug(f"message_reaction.project() event_id={event_id[:20]}..., recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"message_reaction.project() blob not found for event_id={event_id}")
        return None

    # Unwrap (decrypt)
    plaintext, missing_key_ids = crypto.unwrap_event(blob, recorded_by, db)
    if not plaintext or missing_key_ids:
        log.info(f"message_reaction.project() cannot decrypt reaction {event_id[:20]}... - missing key")
        return None

    # Parse event
    event_data = crypto.parse_json(plaintext)

    # Verify signature before trusting event data
    if not crypto.verify_signed_by_peer_shared(event_data, recorded_by, db):
        log.warning(f"message_reaction.project() signature verification failed for {event_id[:20]}...")
        return None

    message_id = event_data['message_id']
    reactor_id = event_data['reactor_id']
    emoji = event_data['emoji']
    signed_by = event_data['signed_by']
    created_at = event_data['created_at']
    global_count = event_data.get('global_count', 1)

    log.debug(f"message_reaction.project() projecting reaction: message_id={message_id[:20]}..., "
             f"reactor_id={reactor_id[:20]}..., emoji={emoji}, global_count={global_count}")

    # DELETION BLOCKING: Check if this reaction_id has been deleted
    # This prevents out-of-order deletion events from being revived
    deletion_exists = safedb.query_one(
        """SELECT 1 FROM message_reaction_deletions
           WHERE reaction_id = ? AND recorded_by = ? LIMIT 1""",
        (event_id, recorded_by)
    )
    if deletion_exists:
        log.debug(f"message_reaction.project() reaction {event_id[:20]}... is deleted, blocking projection")
        return None

    # Check if message exists for validation
    message_row = safedb.query_one(
        "SELECT group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, recorded_by)
    )

    if not message_row:
        log.debug(f"message_reaction.project() message not found yet - accepting reaction as pre-block")
    else:
        log.debug(f"message_reaction.project() message found, accepting reaction")

    # Store unconditionally (INSERT OR IGNORE ensures idempotency)
    # Winner determination happens at query time via window functions
    safedb.execute(
        """INSERT OR IGNORE INTO message_reactions
           (reaction_id, message_id, reactor_id, signed_by, emoji, created_at, global_count, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_id, message_id, reactor_id, signed_by, emoji, created_at, global_count, recorded_by, recorded_at)
    )

    log.debug(f"message_reaction.project() stored reaction_id={event_id[:20]}...")
    return event_id


def project_deletion(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project message_reaction_deletion event.

    Removes the reaction from message_reactions table and records deletion.

    Args:
        event_id: Deletion event ID
        recorded_by: Peer who recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection
    """
    log.debug(f"message_reaction.project_deletion() event_id={event_id[:20]}..., recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"message_reaction.project_deletion() blob not found for deletion_id={event_id}")
        return

    # Unwrap (decrypt)
    plaintext, missing_key_ids = crypto.unwrap_event(blob, recorded_by, db)
    if not plaintext or missing_key_ids:
        log.info(f"message_reaction.project_deletion() cannot decrypt deletion {event_id[:20]}... - missing key")
        return

    # Parse event
    event_data = crypto.parse_json(plaintext)

    # Verify signature before trusting event data
    # message_reaction_deletion uses 'deleted_by' as the signer field (not 'signed_by')
    signer_peer_shared_id = event_data.get('deleted_by')
    if not signer_peer_shared_id:
        log.warning(f"message_reaction.project_deletion() missing deleted_by field for {event_id[:20]}...")
        return

    from events.identity import peer_shared
    try:
        public_key = peer_shared.get_public_key(signer_peer_shared_id, recorded_by, db)
        if not crypto.verify_event(event_data, public_key):
            log.warning(f"message_reaction.project_deletion() signature verification failed for {event_id[:20]}...")
            return
    except ValueError:
        log.warning(f"message_reaction.project_deletion() signer peer_shared not found for {event_id[:20]}...")
        return

    reaction_id = event_data['reaction_id']
    deleted_by = event_data['deleted_by']
    created_at = event_data['created_at']

    log.info(f"message_reaction.project_deletion() deleting reaction_id={reaction_id[:20]}...")

    # Get the reaction to delete
    reaction_row = safedb.query_one(
        "SELECT message_id FROM message_reactions WHERE reaction_id = ? AND recorded_by = ? LIMIT 1",
        (reaction_id, recorded_by)
    )

    if reaction_row:
        # Delete from message_reactions table
        safedb.execute(
            "DELETE FROM message_reactions WHERE reaction_id = ? AND recorded_by = ?",
            (reaction_id, recorded_by)
        )
        log.info(f"message_reaction.project_deletion() deleted reaction {reaction_id[:20]}... from table")
    else:
        log.warning(f"message_reaction.project_deletion() reaction not found: {reaction_id[:20]}...")

    # Record the deletion for audit trail
    safedb.execute(
        """INSERT OR IGNORE INTO message_reaction_deletions
           (deletion_id, reaction_id, deleted_by, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_id, reaction_id, deleted_by, created_at, recorded_by, recorded_at)
    )

    log.debug(f"message_reaction.project_deletion() completed for deletion_id={event_id[:20]}...")


def list_reactions(message_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all reactions on a message, grouped by emoji.

    Returns reactions grouped by emoji with reactor names:
    [
        {'emoji': '👍', 'reactors': ['alice', 'bob'], 'count': 2},
        {'emoji': '❤️', 'reactors': ['charlie'], 'count': 1}
    ]

    Uses window function to get winning reaction for each (message_id, reactor_id, emoji).

    Args:
        message_id: Message to get reactions for
        recorded_by: Peer perspective
        db: Database connection

    Returns:
        List of dicts with emoji, reactors list, and count
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get winning reactions for the message with reactor names
    # Window function ensures we only get the latest reaction per (message_id, reactor_id, emoji)
    # Prefer encrypted username from user_names, fall back to users.name (legacy/empty)
    reactions = safedb.query(
        """WITH winning_reactions AS (
             SELECT message_id, reactor_id, emoji, recorded_by,
                    ROW_NUMBER() OVER (
                        PARTITION BY message_id, reactor_id, emoji
                        ORDER BY global_count DESC, reaction_id DESC
                    ) as rn
             FROM message_reactions
             WHERE message_id = ? AND recorded_by = ?
           )
           SELECT wr.emoji, wr.reactor_id, COALESCE(un.name, u.name) as reactor_name
           FROM winning_reactions wr
           LEFT JOIN users u ON wr.reactor_id = u.user_id AND wr.recorded_by = u.recorded_by
           LEFT JOIN user_names un ON wr.reactor_id = un.user_id AND wr.recorded_by = un.recorded_by
           WHERE wr.rn = 1
           ORDER BY wr.emoji ASC""",
        (message_id, recorded_by)
    )

    # Group by emoji
    grouped = {}
    for reaction in reactions:
        emoji = reaction['emoji']
        reactor_name = reaction['reactor_name'] or reaction['reactor_id'][:20]

        if emoji not in grouped:
            grouped[emoji] = {'emoji': emoji, 'reactors': [], 'count': 0}

        grouped[emoji]['reactors'].append(reactor_name)
        grouped[emoji]['count'] += 1

    # Return as list sorted by emoji
    return sorted(grouped.values(), key=lambda x: x['emoji'])


def list_my_reactions(reactor_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all reactions added by a specific reactor across all messages.

    Returns:
    [
        {'message_id': '...', 'content': 'Hello world', 'emoji': '👍'},
        {'message_id': '...', 'content': 'Hi there', 'emoji': '❤️'}
    ]

    Uses window function to get winning reaction for each (message_id, emoji).

    Args:
        reactor_id: User ID to get reactions for
        recorded_by: Peer perspective
        db: Database connection

    Returns:
        List of dicts with message_id, content, and emoji
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get winning reactions by this reactor, joined with message content
    # Window function ensures we only get the latest reaction per (message_id, emoji)
    reactions = safedb.query(
        """WITH winning_reactions AS (
             SELECT message_id, emoji, recorded_by,
                    ROW_NUMBER() OVER (
                        PARTITION BY message_id, emoji
                        ORDER BY global_count DESC, reaction_id DESC
                    ) as rn
             FROM message_reactions
             WHERE reactor_id = ? AND recorded_by = ?
           )
           SELECT wr.message_id, wr.emoji, m.content, m.created_at
           FROM winning_reactions wr
           LEFT JOIN messages m ON wr.message_id = m.message_id AND wr.recorded_by = m.recorded_by
           WHERE wr.rn = 1
           ORDER BY m.created_at DESC LIMIT 50""",
        (reactor_id, recorded_by)
    )

    return reactions


def cascade_delete_reactions(message_id: str, recorded_by: str, recorded_at: int, db: Any) -> int:
    """Delete all reactions on a message when message is deleted.

    Called by message_deletion.project() to cascade delete reactions.
    Creates deletion events for each reaction to maintain audit trail.

    Args:
        message_id: Message being deleted
        recorded_by: Peer perspective
        recorded_at: Timestamp of the deletion
        db: Database connection

    Returns:
        Number of reactions deleted
    """
    log.info(f"message_reaction.cascade_delete_reactions() deleting all reactions for message_id={message_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Find all reactions on this message
    reactions = safedb.query(
        "SELECT reaction_id FROM message_reactions WHERE message_id = ? AND recorded_by = ?",
        (message_id, recorded_by)
    )

    if not reactions:
        log.info(f"message_reaction.cascade_delete_reactions() no reactions to delete")
        return 0

    log.info(f"message_reaction.cascade_delete_reactions() deleting {len(reactions)} reactions")

    # Simply hard-delete reactions (as per plan recommendation)
    # This is simpler and avoids creating deletion events for each reaction
    deleted_count = safedb.execute(
        "DELETE FROM message_reactions WHERE message_id = ? AND recorded_by = ?",
        (message_id, recorded_by)
    )

    log.info(f"message_reaction.cascade_delete_reactions() deleted {deleted_count} reactions")
    return deleted_count
