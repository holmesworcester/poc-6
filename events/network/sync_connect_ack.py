"""sync_connect_ack event type (LOCAL-ONLY) for connection handshake acknowledgement.

This module handles the second phase of the two-way connection handshake.
When a peer receives a sync_connect, it sends sync_connect_ack back containing
its own transit_key wrapped with the sender's transit_key.

This completes the bidirectional key exchange needed for encrypted sync.

Now uses the connection module for peer-scoped connection management.
"""

# Registry metadata
EVENT_TYPE = 'sync_connect_ack'
SHAREABLE = False  # Local-only - ack is per-peer
EPHEMERAL = True   # Drop if deps missing - sender will retry
PROJECTION_TABLE = None

import logging
from typing import Any
from db import create_safe_db, create_unsafe_db
import crypto
import store
import connection as conn_module

log = logging.getLogger(__name__)


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project sync_connect_ack event: extract their transit_key and update connection.

    No signature verification needed - implicit auth via decryption.
    If we can decrypt it, it came from the peer we sent our transit_key to.

    Args:
        event_id: The sync_connect_ack event ID
        recorded_by: Local peer who received this ack
        recorded_at: When received
        db: Database connection
    """
    log.debug(f"sync_connect_ack.project: event_id={event_id[:20]}... recorded_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"sync_connect_ack.project: blob not found")
        return None

    event_data = crypto.parse_json(blob)

    # Extract for_transit_key_id (the key ID we sent, echoed back for secure matching)
    for_transit_key_id = event_data.get('for_transit_key_id')
    from_peer_shared_id = event_data.get('from_peer_shared_id')  # For logging only, not trusted
    transit_key_id = event_data.get('transit_key_id')
    transit_key_b64 = event_data.get('transit_key')
    transit_key_bytes = crypto.b64decode(transit_key_b64) if transit_key_b64 else None

    if not for_transit_key_id or not transit_key_id or not transit_key_bytes:
        log.warning(f"sync_connect_ack.project: missing required fields (for_transit_key_id={bool(for_transit_key_id)}, transit_key_id={bool(transit_key_id)}, transit_key={bool(transit_key_bytes)})")
        return None

    # Match by OUR transit_key_id that we sent (secure - we know who we sent it to)
    # This prevents a malicious peer from claiming to be someone else in their ack
    # Now uses peer-scoped connections table
    existing_conn = safedb.query_one("""
        SELECT peer_shared_id, invite_id FROM connections
        WHERE our_transit_key_id = ? AND recorded_by = ?
    """, (for_transit_key_id, recorded_by))

    if not existing_conn:
        log.warning(f"sync_connect_ack.project: no connection found for transit_key_id {for_transit_key_id[:20]}... recorded_by={recorded_by[:20]}...")
        return None

    peer_shared_id = existing_conn['peer_shared_id']
    invite_id = existing_conn['invite_id']

    # Update connection with their transit_key via connection module
    conn_module.upsert_connection(
        our_transit_key_id=for_transit_key_id,
        recorded_by=recorded_by,
        peer_shared_id=peer_shared_id,
        invite_id=invite_id,
        their_transit_key_id=transit_key_id,
        their_transit_key=transit_key_bytes,
        t_ms=recorded_at,
        ttl_ms=300000,  # 5 minutes default TTL
        db=db
    )

    log.warning(f"[SYNC_CONNECT_ACK_RECEIVED] from={from_peer_shared_id[:10] if from_peer_shared_id else '?'}... matched_peer={peer_shared_id[:10] if peer_shared_id else invite_id[:10] if invite_id else '?'}... recorded_by={recorded_by[:10]}... UPDATING_CONNECTION")

    return event_id
