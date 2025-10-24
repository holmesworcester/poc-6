"""Purge expired event type for forward secrecy.

Deletes all events with ttl_ms <= cutoff_ms from the event store and projections.
This event itself is permanent (no TTL) to maintain audit trail of purging operations.
"""
from typing import Any
import logging
import json
import crypto
import store
from db import create_unsafe_db, create_safe_db

log = logging.getLogger(__name__)


def create(peer_id: str, cutoff_ms: int, t_ms: int, db: Any) -> str:
    """Create a purge_expired event that records deletion of expired events.

    The purge_expired event is immutable and permanent - it documents what was purged and when.
    Actual deletion happens during projection.

    Args:
        peer_id: Peer creating the purge event
        cutoff_ms: Absolute timestamp - delete all events with ttl_ms <= cutoff_ms (where ttl_ms > 0)
        t_ms: When this purge_expired event is created
        db: Database connection (caller handles commit)

    Returns:
        purge_expired_id: The stored purge_expired event ID

    Example:
        >>> purge_id = purge_expired.create(alice_peer_id, cutoff_ms=1600000000000, t_ms=1600100000000, db=db)
        >>> db.commit()
    """
    log.info(f"purge_expired.create() peer_id={peer_id[:20]}..., cutoff_ms={cutoff_ms}, t_ms={t_ms}")

    # Create event data (plaintext, no encryption for audit trail)
    event_data = {
        'type': 'purge_expired',
        'cutoff_ms': cutoff_ms,
        'created_by': peer_id,
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store the event with recorded wrapper
    purge_expired_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"purge_expired.create() created purge_expired_id={purge_expired_id[:20]}...")
    return purge_expired_id


def project(purge_expired_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project purge_expired event - delete all expired events from store and projections.

    This is the actual purging operation:
    1. Get list of all event IDs with ttl_ms <= cutoff_ms (where ttl_ms > 0)
    2. Delete from store table (the actual blobs)
    3. Remove from valid_events
    4. Cascade delete dependent events
    5. Record the purge_expired event itself as valid

    Args:
        purge_expired_id: The purge_expired event ID
        recorded_by: Peer who recorded this purge
        recorded_at: When this peer recorded it
        db: Database connection

    Returns:
        purge_expired_id if successful, None if validation failed
    """
    log.info(f"purge_expired.project() purge_expired_id={purge_expired_id[:20]}..., recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get purge_expired event from store
    blob = store.get(purge_expired_id, unsafedb)
    if not blob:
        log.warning(f"purge_expired.project() blob not found for purge_expired_id={purge_expired_id}")
        return None

    # Parse event data
    event_data = crypto.parse_json(blob)
    cutoff_ms = event_data['cutoff_ms']

    log.info(f"purge_expired.project() purging all events with ttl_ms <= {cutoff_ms}")

    # Get all events from store that have expired (ttl_ms > 0 and ttl_ms <= cutoff_ms)
    # We query from store directly because we need to know event IDs and check their ttl_ms
    # This is tricky because ttl_ms is in projection tables, not in store
    # Strategy: check projection tables for expired events, then delete from store

    expired_event_ids = []

    # Check messages table
    messages_with_ttl = safedb.query(
        """SELECT DISTINCT message_id FROM messages
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )
    expired_event_ids.extend([row['message_id'] for row in messages_with_ttl])
    log.info(f"purge_expired.project() found {len(messages_with_ttl)} expired messages")

    # Check message_attachments table
    attachments_with_ttl = safedb.query(
        """SELECT DISTINCT message_id FROM message_attachments
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )
    # Note: message_attachment_id might not be trackable, so we mark by message_id
    # In practice, deleting the message will cascade delete attachments

    # Check files table
    files_with_ttl = safedb.query(
        """SELECT DISTINCT file_id FROM files
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )
    expired_event_ids.extend([row['file_id'] for row in files_with_ttl])
    log.info(f"purge_expired.project() found {len(files_with_ttl)} expired files")

    # Check file_slices table
    slices_with_ttl = safedb.query(
        """SELECT DISTINCT file_id FROM file_slices
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )
    # Slices are cleaned up by their file, no need to track separately

    # Check transit prekeys
    transit_prekeys_with_ttl = safedb.query(
        """SELECT transit_prekey_id FROM transit_prekeys
           WHERE ttl_ms > 0 AND ttl_ms <= ?""",
        (cutoff_ms,)
    )
    expired_event_ids.extend([row['transit_prekey_id'] for row in transit_prekeys_with_ttl])
    log.info(f"purge_expired.project() found {len(transit_prekeys_with_ttl)} expired transit prekeys")

    # Check group prekeys (subjective)
    group_prekeys_with_ttl = safedb.query(
        """SELECT prekey_id FROM group_prekeys
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )
    expired_event_ids.extend([row['prekey_id'] for row in group_prekeys_with_ttl])
    log.info(f"purge_expired.project() found {len(group_prekeys_with_ttl)} expired group prekeys")

    # Check transit_prekeys_shared (subjective)
    transit_shared_with_ttl = safedb.query(
        """SELECT transit_prekey_shared_id FROM transit_prekeys_shared
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )
    expired_event_ids.extend([row['transit_prekey_shared_id'] for row in transit_shared_with_ttl])
    log.info(f"purge_expired.project() found {len(transit_shared_with_ttl)} expired transit_prekeys_shared")

    # Check group_prekeys_shared (subjective)
    group_shared_with_ttl = safedb.query(
        """SELECT group_prekey_shared_id FROM group_prekeys_shared
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )
    expired_event_ids.extend([row['group_prekey_shared_id'] for row in group_shared_with_ttl])
    log.info(f"purge_expired.project() found {len(group_shared_with_ttl)} expired group_prekeys_shared")

    # Check message_rekeys
    rekeys_with_ttl = safedb.query(
        """SELECT rekey_id FROM message_rekeys
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )
    expired_event_ids.extend([row['rekey_id'] for row in rekeys_with_ttl])
    log.info(f"purge_expired.project() found {len(rekeys_with_ttl)} expired message_rekeys")

    # Deduplicate
    expired_event_ids = list(set(expired_event_ids))

    # Delete expired events from store
    for event_id in expired_event_ids:
        unsafedb.execute("DELETE FROM store WHERE id = ?", (event_id,))
        # Remove from valid_events for this peer
        safedb.execute(
            "DELETE FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (event_id, recorded_by)
        )

    log.info(f"purge_expired.project() deleted {len(expired_event_ids)} expired events from store")

    # Also delete from projection tables
    # Messages
    safedb.execute(
        """DELETE FROM messages
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )

    # Message attachments
    safedb.execute(
        """DELETE FROM message_attachments
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )

    # Files
    safedb.execute(
        """DELETE FROM files
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )

    # File slices
    safedb.execute(
        """DELETE FROM file_slices
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )

    # Transit prekeys (device-wide, not scoped by recorded_by)
    unsafedb.execute(
        "DELETE FROM transit_prekeys WHERE ttl_ms > 0 AND ttl_ms <= ?",
        (cutoff_ms,)
    )

    # Group prekeys
    safedb.execute(
        """DELETE FROM group_prekeys
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )

    # Transit prekeys shared
    safedb.execute(
        """DELETE FROM transit_prekeys_shared
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )

    # Group prekeys shared
    safedb.execute(
        """DELETE FROM group_prekeys_shared
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )

    # Message rekeys
    safedb.execute(
        """DELETE FROM message_rekeys
           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?""",
        (recorded_by, cutoff_ms)
    )

    # Mark the purge_expired event itself as valid (it has no TTL)
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (purge_expired_id, recorded_by)
    )

    log.info(f"purge_expired.project() successfully completed purge_expired")
    return purge_expired_id
