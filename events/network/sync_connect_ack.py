"""sync_connect_ack event type (LOCAL-ONLY) for connection handshake acknowledgement.

This module handles the second phase of the two-way connection handshake.
When a peer receives a sync_connect, it sends sync_connect_ack back containing
its own transit_key wrapped with the sender's transit_key.

This completes the bidirectional key exchange needed for encrypted sync.
"""

# Registry metadata
EVENT_TYPE = 'sync_connect_ack'
SHAREABLE = False  # Local-only - ack is per-peer
EPHEMERAL = True   # Drop if deps missing - sender will retry
PROJECTION_TABLE = None

import logging
from typing import Any
from db import create_unsafe_db
import crypto
import store

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

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"sync_connect_ack.project: blob not found")
        return None

    event_data = crypto.parse_json(blob)

    # Extract for_transit_key_id (the key ID we sent, echoed back for secure matching)
    for_transit_key_id = event_data.get('for_transit_key_id')
    transit_key_id = event_data.get('transit_key_id')
    transit_key_b64 = event_data.get('transit_key')
    transit_key_bytes = crypto.b64decode(transit_key_b64) if transit_key_b64 else None

    if not for_transit_key_id or not transit_key_id or not transit_key_bytes:
        log.warning(f"sync_connect_ack.project: missing required fields (for_transit_key_id={bool(for_transit_key_id)}, transit_key_id={bool(transit_key_id)}, transit_key={bool(transit_key_bytes)})")
        return None

    # Match by OUR transit_key_id that we sent (secure - we know who we sent it to)
    # This prevents a malicious peer from claiming to be someone else in their ack
    existing_conn = unsafedb.query_one("""
        SELECT peer_shared_id FROM sync_connections WHERE our_transit_key_id = ?
    """, (for_transit_key_id,))

    if not existing_conn:
        log.warning(f"sync_connect_ack.project: no connection found for transit_key_id {for_transit_key_id[:20]}...")
        return None

    peer_shared_id = existing_conn['peer_shared_id']

    # Update connection with their transit_key_id and transit_key
    unsafedb.execute("""
        UPDATE sync_connections
        SET their_transit_key_id = ?, their_transit_key = ?, last_seen_ms = ?, ttl_ms = ?
        WHERE peer_shared_id = ?
    """, (
        transit_key_id,
        transit_key_bytes,
        recorded_at,
        300000,  # 5 minutes default TTL
        peer_shared_id
    ))

    log.warning(f"[SYNC_CONNECT_ACK_RECEIVED] matched_peer={peer_shared_id[:10]}... recorded_by={recorded_by[:10]}... UPDATING_CONNECTION")

    return event_id
