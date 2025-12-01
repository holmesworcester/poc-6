"""Message deletion event type.

Pure functions:
    create(deps, t_ms) -> CreateResult
    project(input_dict) -> ProjectorResult

API functions:
    delete(peer_id, message_id, t_ms, db) -> str
    project_event(deletion_id, recorded_by, recorded_at, db) -> str | None
    validate(message_id, deleted_by, recorded_by, db) -> bool

Forward Secrecy:
    run_message_purge_cycle(peer_id, t_ms, db) -> dict
    run_message_purge_cycle_for_all_peers(t_ms, db) -> dict
"""
from typing import Any, TypedDict
import logging
import crypto
import store
from db import create_safe_db, create_unsafe_db
from events.identity import peer
from events.group import group

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class MessageDeletionEventData(TypedDict):
    type: str
    message_id: str
    signed_by: str
    created_at: int


class CreateDeps(TypedDict):
    """Dependencies resolved for message_deletion creation."""
    peer_shared_id: str
    private_key: bytes
    key_data: dict
    message_id: str
    group_id: str


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def create(deps: CreateDeps, t_ms: int):
    """Pure function to create a message_deletion event.

    Args:
        deps: Resolved dependencies
        t_ms: Timestamp

    Returns:
        CreateResult with deletion blob
    """
    from projection import CreateResult, BlobSpec, compute_event_id

    event_data = {
        'type': 'message_deletion',
        'message_id': deps['message_id'],
        'signed_by': deps['peer_shared_id'],
        'created_at': t_ms,
    }

    signed_event = crypto.sign_event(event_data, deps['private_key'])
    canonical = crypto.canonicalize_json(signed_event)
    blob = crypto.wrap(canonical, deps['key_data'], None)
    deletion_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=deletion_id, event_type='message_deletion')],
        primary_id=deletion_id,
    )


def project(input_dict: dict):
    """Pure projection: dict -> result.

    Authorization: deleter must be author OR admin.
    Output: deleted_events=[message_id] - framework handles cascade.
    """
    from projection import ProjectorResult

    event_data = input_dict["event_data"]
    deps = input_dict.get("dependencies", {})

    message_id = event_data.get("message_id")
    deleted_by = event_data.get("signed_by")

    if not message_id or not deleted_by:
        return ProjectorResult(valid=False, reason="missing message_id or signed_by")

    # Get message for authorization
    message = deps.get("message")
    if not message:
        return ProjectorResult(blocked=True, missing_deps=["message"])

    # Authorization: deleted_by must be author OR admin
    message_author = message.get("author_id")
    is_author = (deleted_by == message_author)
    is_admin = deps.get("is_admin", False)

    if not is_author and not is_admin:
        log.warning(f"message_deletion: not authorized. deleted_by={deleted_by[:20]}...")
        return ProjectorResult(valid=False, reason="not authorized to delete")

    return ProjectorResult(
        valid=True,
        deleted_events=[message_id],
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    message_id: str = "msg_123",
    signed_by: str = "peer_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "message_deletion",
        "message_id": message_id,
        "signed_by": signed_by,
        "created_at": created_at,
    }


def make_message_dep(
    message_id: str = "msg_123",
    author_id: str = "peer_123",
    group_id: str = "grp_123",
    channel_id: str = "ch_123",
) -> dict:
    """Build message dependency for testing."""
    return {
        "message_id": message_id,
        "author_id": author_id,
        "group_id": group_id,
        "channel_id": channel_id,
    }


def make_input(
    event_id: str = "del_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
    recorded_at: int = 1000001,
    message_event: dict | None = None,
    is_admin: bool = False,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {
            "message": message_event,
            "is_admin": is_admin,
        },
    }


# ============================================================================
# API FUNCTIONS
# ============================================================================

def validate(message_id: str, deleted_by: str, recorded_by: str, db: Any) -> bool:
    """Validate that deleted_by has authorization to delete the message.

    Authorization rules:
    1. deleted_by is the message author, OR
    2. deleted_by is an admin
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Check if deleted_by is the message author
    message_row = safedb.query_one(
        "SELECT author_id, group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
        (message_id, recorded_by)
    )

    if message_row:
        if deleted_by == message_row['author_id']:
            return True

    # Check if deleted_by is an admin
    from events.identity import invite
    return invite.is_admin(deleted_by, recorded_by, db)


def delete(peer_id: str, message_id: str, t_ms: int, db: Any) -> str:
    """Delete a message.

    Args:
        peer_id: Local peer ID
        message_id: Message to delete
        t_ms: Timestamp
        db: Database connection

    Returns:
        deletion_id

    Raises:
        ValueError: If not authorized
    """
    from projection import store_create_result

    log.info(f"message_deletion.delete() message_id={message_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get message info
    message_row = safedb.query_one(
        "SELECT author_id, group_id FROM messages WHERE message_id = ? AND recorded_by = ?",
        (message_id, peer_id)
    )
    if not message_row:
        raise ValueError(f"Message {message_id} not found")

    # Get peer info
    peer_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (peer_id, peer_id)
    )
    if not peer_row:
        raise ValueError(f"Peer {peer_id} not found")

    # Authorization check
    if not validate(message_id, peer_row['peer_shared_id'], peer_id, db):
        raise ValueError(f"Not authorized to delete message {message_id}")

    # Resolve dependencies
    deps = {
        'peer_shared_id': peer_row['peer_shared_id'],
        'private_key': peer.get_private_key(peer_id, peer_id, db),
        'key_data': group.pick_key(message_row['group_id'], peer_id, db),
        'message_id': message_id,
        'group_id': message_row['group_id'],
    }

    # Pure create
    result = create(deps, t_ms)

    # Store and project
    deletion_id = store_create_result(result, peer_id, t_ms, db)

    log.info(f"message_deletion.delete() created deletion_id={deletion_id[:20]}...")
    return deletion_id


def project_event(deletion_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project a message_deletion event.

    Handles:
    - Signature verification
    - Authorization check via pure project()
    - Forward secrecy key purging
    - Blob deletion from store
    """
    from projection import apply_result, cleanup_deleted_events
    from events.identity import peer_shared

    log.info(f"message_deletion.project_event() deletion_id={deletion_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # 1. Get and decrypt blob
    blob = store.get(deletion_id, unsafedb)
    if not blob:
        return None

    unwrapped, _ = crypto.unwrap(blob, recorded_by, db)
    if not unwrapped:
        return None

    event_data = crypto.parse_json(unwrapped)

    # 2. Verify peer_shared signature
    signed_by = event_data.get("signed_by")
    if signed_by:
        try:
            public_key = peer_shared.get_public_key(signed_by, recorded_by, db)
            if not crypto.verify_event(event_data, public_key):
                log.warning(f"Invalid signature for message_deletion {deletion_id}")
                return None
        except (ValueError, KeyError):
            return None

    # 3. Resolve message dependency
    message_id = event_data.get("message_id")
    message = None
    if message_id:
        message_row = safedb.query_one(
            "SELECT author_id, group_id, channel_id FROM messages WHERE message_id = ? AND recorded_by = ?",
            (message_id, recorded_by)
        )
        if message_row:
            message = {
                "message_id": message_id,
                "author_id": message_row["author_id"],
                "group_id": message_row["group_id"],
                "channel_id": message_row["channel_id"],
            }

    # 4. Check admin status
    from events.identity import invite
    is_admin = invite.is_admin(signed_by, recorded_by, db) if signed_by else False

    # 5. Build input and call pure projector
    input_dict = {
        "event_id": deletion_id,
        "event_data": event_data,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {
            "message": message,
            "is_admin": is_admin,
        },
    }

    result = project(input_dict)

    if result.blocked:
        log.info(f"message_deletion.project_event() blocked: {result.missing_deps}")
        return None

    if not result.valid:
        log.warning(f"message_deletion.project_event() rejected: {result.reason}")
        return None

    # Apply result
    apply_result(result, recorded_by, recorded_at, db)

    # Forward secrecy: mark key for purging
    if message_id:
        message_blob = store.get(message_id, unsafedb)
        if message_blob:
            try:
                key_id_bytes = message_blob[:crypto.ID_SIZE]
                key_id_b64 = crypto.b64encode(key_id_bytes)
                safedb.execute(
                    "INSERT OR IGNORE INTO keys_to_purge (key_id, marked_at, recorded_by) VALUES (?, ?, ?)",
                    (key_id_b64, recorded_at, recorded_by)
                )
            except Exception as e:
                log.warning(f"Failed to mark key for purging: {e}")

    # Delete message blob
    if message_id:
        unsafedb.execute("DELETE FROM store WHERE id = ?", (message_id,))
        safedb.execute(
            "DELETE FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
            (message_id, recorded_by)
        )

    # Cascade cleanup
    cleanup_deleted_events(recorded_by, db)

    log.info(f"message_deletion.project_event() completed deletion_id={deletion_id[:20]}...")
    return deletion_id


# ============================================================================
# FORWARD SECRECY
# ============================================================================

def run_message_purge_cycle(peer_id: str, t_ms: int, db: Any) -> dict[str, Any]:
    """Execute forward secrecy purge cycle for messages with deleted content."""
    log.info(f"message_deletion.run_message_purge_cycle() peer={peer_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=peer_id)

    stats = {'messages_rekeyed': 0, 'keys_purged': 0, 'errors': []}

    purge_keys = safedb.query(
        "SELECT key_id FROM keys_to_purge WHERE recorded_by = ? ORDER BY marked_at ASC",
        (peer_id,)
    )

    if not purge_keys:
        return stats

    from events.content import message_rekey
    from events.group import group_key

    for purge_key_row in purge_keys:
        purge_key_id = purge_key_row['key_id']

        messages_rows = safedb.query(
            """SELECT message_id FROM messages
               WHERE recorded_by = ? AND key_id = ?
               AND message_id NOT IN (SELECT event_id FROM deleted_events WHERE recorded_by = ?)""",
            (peer_id, purge_key_id, peer_id)
        )
        messages = [r['message_id'] for r in messages_rows]

        if not messages:
            safedb.execute("DELETE FROM group_keys WHERE key_id = ? AND recorded_by = ?", (purge_key_id, peer_id))
            safedb.execute("DELETE FROM keys_to_purge WHERE key_id = ? AND recorded_by = ?", (purge_key_id, peer_id))
            stats['keys_purged'] += 1
            continue

        try:
            msg_row = safedb.query_one(
                "SELECT group_id FROM messages WHERE message_id = ? AND recorded_by = ? LIMIT 1",
                (messages[0], peer_id)
            )
            if not msg_row:
                continue

            clean_key_id = group_key.get_or_create_clean_key(msg_row['group_id'], peer_id, t_ms, db)
        except Exception as e:
            stats['errors'].append(str(e))
            continue

        for message_id in messages:
            try:
                rekey_id = message_rekey.create(message_id, clean_key_id, peer_id, t_ms, db)
                message_rekey.project_event(rekey_id, peer_id, t_ms, db)
                stats['messages_rekeyed'] += 1
            except Exception as e:
                stats['errors'].append(str(e))

        safedb.execute("DELETE FROM group_keys WHERE key_id = ? AND recorded_by = ?", (purge_key_id, peer_id))
        safedb.execute("DELETE FROM keys_to_purge WHERE key_id = ? AND recorded_by = ?", (purge_key_id, peer_id))
        stats['keys_purged'] += 1

    return stats


def run_message_purge_cycle_for_all_peers(t_ms: int, db: Any) -> dict[str, Any]:
    """Run message purge cycle for all local peers."""
    unsafedb = create_unsafe_db(db)

    stats = {'peers_processed': 0, 'total_messages_rekeyed': 0, 'total_keys_purged': 0, 'errors': []}

    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    for peer_row in local_peers:
        try:
            peer_stats = run_message_purge_cycle(peer_row['peer_id'], t_ms, db)
            stats['peers_processed'] += 1
            stats['total_messages_rekeyed'] += peer_stats['messages_rekeyed']
            stats['total_keys_purged'] += peer_stats['keys_purged']
            stats['errors'].extend(peer_stats['errors'])
        except Exception as e:
            stats['errors'].append(str(e))

    return stats
