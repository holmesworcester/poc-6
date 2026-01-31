"""Purge expired event type for forward secrecy.

Deletes all events with ttl_ms <= cutoff_ms from the event store and projections.
This event itself is permanent (no TTL) to maintain audit trail of purging operations.
"""
from typing import Any
import logging
import json
from . import crypto
from . import store
from .db import create_unsafe_db, create_safe_db

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
        'signed_by': peer_id,
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store without projection (purge_expired is local-only audit data)
    purge_expired_id = store.batch_store_events([blob], peer_id, t_ms, db)[0]

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

    # Table-driven configuration for querying expired items
    PURGEABLE_TABLES = [
        {
            'table': 'messages',
            'id_col': 'message_id',
            'db': 'safe',
            'name': 'messages',
            'has_key_id': True,  # Special handling for forward secrecy
        },
        {
            'table': 'connection_pubkeys',
            'id_col': 'connection_pubkey_id',
            'db': 'unsafe',  # Device-wide, not subjective
            'name': 'connection pubkeys',
        },
        {
            'table': 'connection_pubkeys_shared',
            'id_col': 'connection_pubkey_shared_id',
            'db': 'safe',
            'name': 'connection_pubkeys_shared',
        },
        # NOTE: group_prekeys, group_prekeys_shared removed - use pubkey/pubkey_shared
        {
            'table': 'message_rekeys',
            'id_col': 'rekey_id',
            'db': 'safe',
            'name': 'message_rekeys',
        },
    ]

    expired_event_ids = []

    # Query expired items from each table
    for config in PURGEABLE_TABLES:
        table_db = safedb if config['db'] == 'safe' else unsafedb
        table = config['table']
        id_col = config['id_col']
        name = config['name']

        # Build query based on whether table is scoped by recorded_by
        if config['db'] == 'safe':
            # Special handling for messages to get key_id for forward secrecy
            if config.get('has_key_id'):
                query = f"""SELECT DISTINCT {id_col}, key_id FROM {table}
                           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?"""
                rows = table_db.query(query, (recorded_by, cutoff_ms))

                # Mark encryption keys for purging (forward secrecy)
                for row in rows:
                    if row.get('key_id'):
                        try:
                            safedb.execute(
                                """INSERT OR IGNORE INTO keys_to_purge (key_id, marked_at, recorded_by)
                                   VALUES (?, ?, ?)""",
                                (row['key_id'], recorded_at, recorded_by)
                            )
                            log.debug(f"purge_expired.project() marked key {row['key_id'][:20]}... for purging (expired message)")
                        except Exception as e:
                            log.warning(f"purge_expired.project() failed to mark key for purging: {e}")

                keys_marked = len([r for r in rows if r.get('key_id')])
                log.info(f"purge_expired.project() found {len(rows)} expired {name}, marked {keys_marked} keys for purging")
            else:
                query = f"""SELECT {id_col} FROM {table}
                           WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?"""
                rows = table_db.query(query, (recorded_by, cutoff_ms))
                log.info(f"purge_expired.project() found {len(rows)} expired {name}")
        else:
            # Unsafe tables are not scoped by recorded_by
            query = f"SELECT {id_col} FROM {table} WHERE ttl_ms > 0 AND ttl_ms <= ?"
            rows = table_db.query(query, (cutoff_ms,))
            log.info(f"purge_expired.project() found {len(rows)} expired {name}")

        expired_event_ids.extend([row[id_col] for row in rows])

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

    # Also delete from projection tables (table-driven)
    # Add message_attachments and file_slices to the list for deletion
    PROJECTION_TABLES_TO_CLEAN = PURGEABLE_TABLES + [
        {'table': 'message_attachments', 'db': 'safe', 'name': 'message_attachments'},
        {'table': 'file_slices', 'db': 'safe', 'name': 'file_slices'},
    ]

    for config in PROJECTION_TABLES_TO_CLEAN:
        table_db = safedb if config['db'] == 'safe' else unsafedb
        table = config['table']

        if config['db'] == 'safe':
            table_db.execute(
                f"DELETE FROM {table} WHERE recorded_by = ? AND ttl_ms > 0 AND ttl_ms <= ?",
                (recorded_by, cutoff_ms)
            )
        else:
            table_db.execute(
                f"DELETE FROM {table} WHERE ttl_ms > 0 AND ttl_ms <= ?",
                (cutoff_ms,)
            )

    # Mark the purge_expired event itself as valid (it has no TTL)
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (purge_expired_id, recorded_by)
    )

    log.info(f"purge_expired.project() successfully completed purge_expired")
    return purge_expired_id


def run_purge_expired_for_all_peers(t_ms: int, db: Any) -> dict[str, Any]:
    """Run purge_expired for all local peers in the database.

    This is the recurring job that should be called periodically to clean up
    expired events based on their TTL. It:
    1. Gets all local peers
    2. For each peer, creates a purge_expired event for cutoff = t_ms
    3. Projects the purge_expired event to actually delete expired events

    Args:
        t_ms: Current time in milliseconds (cutoff for expiry)
        db: Database connection (caller must commit)

    Returns:
        Dict with stats: {
            'peers_processed': int,
            'purge_events_created': list[str],  # purge_expired_id's
            'errors': list[str]
        }

    Error Handling:
        - If an error occurs for one peer, it is logged and added to the
          'errors' list, but processing continues for other peers
        - Each peer's changes are committed by the caller, so partial failures
          leave some peers purged and others unchanged
        - If projection fails for a peer, that peer's purge_expired event is
          still created but not marked valid (will retry on next run)
    """
    log.info(f"purge_expired.run_purge_expired_for_all_peers() t_ms={t_ms}")

    unsafedb = create_unsafe_db(db)

    stats = {
        'peers_processed': 0,
        'purge_events_created': [],
        'errors': []
    }

    # Get all local peers
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")

    if not local_peer_rows:
        log.info(f"purge_expired.run_purge_expired_for_all_peers() no local peers found")
        return stats

    log.info(f"purge_expired.run_purge_expired_for_all_peers() found {len(local_peer_rows)} local peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']
        try:
            # Create purge_expired event for this peer
            purge_expired_id = create(peer_id, cutoff_ms=t_ms, t_ms=t_ms, db=db)
            log.info(f"purge_expired.run_purge_expired_for_all_peers() created purge_expired_id={purge_expired_id[:20]}... for peer {peer_id[:20]}...")

            # Immediately project it to actually delete expired events
            result = project(purge_expired_id, recorded_by=peer_id, recorded_at=t_ms, db=db)

            if result:
                stats['purge_events_created'].append(purge_expired_id)
                stats['peers_processed'] += 1
                log.info(f"purge_expired.run_purge_expired_for_all_peers() successfully purged expired events for peer {peer_id[:20]}...")
            else:
                error = f"Failed to project purge_expired for peer {peer_id[:20]}..."
                log.warning(f"purge_expired.run_purge_expired_for_all_peers() {error}")
                stats['errors'].append(error)

        except Exception as e:
            error = f"Error processing peer {peer_id[:20]}...: {e}"
            log.error(f"purge_expired.run_purge_expired_for_all_peers() {error}")
            stats['errors'].append(error)
            continue

    log.info(f"purge_expired.run_purge_expired_for_all_peers() complete: {stats['peers_processed']} peers processed")
    return stats
