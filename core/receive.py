"""Receive and process incoming transit blobs.

This module handles the receive side of the sync protocol:
- Draining incoming packets from the queue
- Unwrapping transit-encrypted blobs
- Routing blobs to the correct local peers
- Creating recorded events for projection
"""

from typing import Any
import os
import json
from core.db import create_safe_db, create_unsafe_db
from core import queues
from core import crypto
from core import store
import logging

log = logging.getLogger(__name__)


_WRITE_BEHIND_BUFFER_FAST: list[dict[str, Any]] = []
_WRITE_BEHIND_BUFFER_SLOW: list[dict[str, Any]] = []
_WRITE_BEHIND_FAST_COUNT = 0
_WRITE_BEHIND_SLOW_COUNT = 0
_WRITE_BEHIND_FAST_START_MS: int | None = None
_WRITE_BEHIND_SLOW_START_MS: int | None = None


def _write_behind_enabled() -> bool:
    return os.getenv("SYNC_PERF_WRITE_BEHIND") == "1"


def _write_behind_should_flush(t_ms: int) -> bool:
    max_events = int(os.getenv("SYNC_PERF_WRITE_BEHIND_MAX_EVENTS", "0"))
    max_ms = int(os.getenv("SYNC_PERF_WRITE_BEHIND_MAX_MS", "0"))
    force_flush = os.getenv("SYNC_PERF_WRITE_BEHIND_FLUSH") == "1"

    if force_flush:
        return True
    if max_events and _WRITE_BEHIND_FAST_COUNT >= max_events:
        return True
    if max_ms and _WRITE_BEHIND_FAST_START_MS is not None and t_ms - _WRITE_BEHIND_FAST_START_MS >= max_ms:
        return True
    return False


def _file_slice_should_flush(t_ms: int) -> bool:
    max_events = int(os.getenv("SYNC_PERF_FILE_SLICE_MAX_EVENTS", "0"))
    max_ms = int(os.getenv("SYNC_PERF_FILE_SLICE_MAX_MS", "0"))
    force_flush = os.getenv("SYNC_PERF_FILE_SLICE_FLUSH") == "1"

    if force_flush:
        return True
    if max_events == 0 and max_ms == 0:
        return _write_behind_should_flush(t_ms)
    if max_events and _WRITE_BEHIND_SLOW_COUNT >= max_events:
        return True
    if max_ms and _WRITE_BEHIND_SLOW_START_MS is not None and t_ms - _WRITE_BEHIND_SLOW_START_MS >= max_ms:
        return True
    return False


def _buffer_write_behind(unwrapped_blob: bytes, recorded_by_peers: list[str],
                         from_addr: tuple[str, int] | None, t_ms: int,
                         is_file_slice: bool) -> None:
    global _WRITE_BEHIND_FAST_COUNT, _WRITE_BEHIND_SLOW_COUNT
    global _WRITE_BEHIND_FAST_START_MS, _WRITE_BEHIND_SLOW_START_MS

    entry = {
        'blob': unwrapped_blob,
        'recorded_by_peers': recorded_by_peers,
        'from_addr': from_addr,
        't_ms': t_ms,
    }

    if is_file_slice:
        _WRITE_BEHIND_BUFFER_SLOW.append(entry)
        _WRITE_BEHIND_SLOW_COUNT += 1
        if _WRITE_BEHIND_SLOW_START_MS is None:
            _WRITE_BEHIND_SLOW_START_MS = t_ms
    else:
        _WRITE_BEHIND_BUFFER_FAST.append(entry)
        _WRITE_BEHIND_FAST_COUNT += 1
        if _WRITE_BEHIND_FAST_START_MS is None:
            _WRITE_BEHIND_FAST_START_MS = t_ms


def _parse_unwrapped_json(unwrapped_blob: bytes) -> dict[str, Any] | None:
    if not unwrapped_blob or unwrapped_blob[:1] != b'{':
        return None
    try:
        return crypto.parse_json(unwrapped_blob)
    except Exception:
        return None


def _handle_negentropy_envelope(envelope: dict[str, Any],
                                recorded_by_peers: list[str],
                                t_ms: int,
                                db: Any) -> bool:
    if envelope.get('type') != 'negentropy':
        return False

    connection_id = envelope.get('reply_connection_id')
    if not connection_id:
        return False

    from events.network import negentropy
    for recorded_by in recorded_by_peers:
        negentropy.handle_incoming(db, recorded_by, connection_id, envelope, t_ms)
    return True


def _flush_write_behind_entries(entries: list[dict[str, Any]], db: Any) -> list[str]:
    """Flush buffered events to the store and return recorded_ids for projection.

    Perf-only write-behind path: stores event + recorded blobs in batch.
    """
    event_rows: list[tuple[str, bytes, int]] = []
    recorded_rows: list[tuple[str, bytes, int]] = []
    recorded_ids: list[str] = []

    for entry in entries:
        blob = entry['blob']
        stored_at = entry['t_ms']
        recorded_by_peers = entry['recorded_by_peers']

        event_id_bytes = crypto.hash(blob)
        event_id = crypto.b64encode(event_id_bytes)
        event_rows.append((event_id, blob, stored_at))

        for recorded_by in recorded_by_peers:
            recorded_blob = json.dumps({
                'type': 'recorded',
                'ref_id': event_id,
                'recorded_by': recorded_by,
            }).encode('utf-8')
            recorded_id = crypto.b64encode(crypto.hash(recorded_blob))
            recorded_rows.append((recorded_id, recorded_blob, stored_at))
            recorded_ids.append(recorded_id)

    db._conn.executemany(
        "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
        event_rows
    )
    db._conn.executemany(
        "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
        recorded_rows
    )

    return list(dict.fromkeys(recorded_ids))


def flush_write_behind(t_ms: int, db: Any, force: bool = False) -> list[str]:
    """Flush buffered events to the store and return recorded_ids for projection."""
    global _WRITE_BEHIND_FAST_COUNT, _WRITE_BEHIND_SLOW_COUNT
    global _WRITE_BEHIND_FAST_START_MS, _WRITE_BEHIND_SLOW_START_MS

    if not _write_behind_enabled():
        return []

    flush_fast = force or _write_behind_should_flush(t_ms)
    flush_slow = force or _file_slice_should_flush(t_ms)

    recorded_ids: list[str] = []

    if flush_fast and _WRITE_BEHIND_BUFFER_FAST:
        recorded_ids.extend(_flush_write_behind_entries(_WRITE_BEHIND_BUFFER_FAST, db))
        _WRITE_BEHIND_BUFFER_FAST.clear()
        _WRITE_BEHIND_FAST_COUNT = 0
        _WRITE_BEHIND_FAST_START_MS = None

    if flush_slow and _WRITE_BEHIND_BUFFER_SLOW:
        recorded_ids.extend(_flush_write_behind_entries(_WRITE_BEHIND_BUFFER_SLOW, db))
        _WRITE_BEHIND_BUFFER_SLOW.clear()
        _WRITE_BEHIND_SLOW_COUNT = 0
        _WRITE_BEHIND_SLOW_START_MS = None

    return list(dict.fromkeys(recorded_ids))

def route_blob_to_peers(blob: bytes, db: Any) -> list[str]:
    """Device-wide routing: determine which local peers can decrypt this blob.

    Checks connections (symmetric), transit keys (symmetric), and transit prekeys
    (asymmetric) to find all local peers who have the decryption key for this blob.

    Args:
        blob: Transit-wrapped blob with hint in first KEY_ID_SIZE bytes
        db: Database connection

    Returns:
        List of peer_ids who can decrypt this blob (empty if no keys found)
    """

    hint = blob[:crypto.KEY_ID_SIZE]
    hint_b64 = crypto.b64encode(hint)

    log.debug(f"route_blob_to_peers: looking for hint_b64={hint_b64}")

    # Try connections first (symmetric keys from connection handshake)
    # The hint is first KEY_ID_SIZE bytes of connection_id
    try:
        cursor = db._conn.execute(
            "SELECT DISTINCT connection_id, recorded_by FROM connections"
        )
        recorded_by_peers = []
        for row in cursor.fetchall():
            conn_id = row[0]
            try:
                conn_id_bytes = crypto.b64decode(conn_id)
                if conn_id_bytes[:crypto.KEY_ID_SIZE] == hint:
                    recorded_by_peers.append(row[1])
            except Exception:
                continue
        if recorded_by_peers:
            log.debug(f"route_blob_to_peers: routed to {len(recorded_by_peers)} peers via connection")
            return recorded_by_peers
    except Exception as e:
        log.warning(f"route_blob_to_peers: Failed to query connections: {e}")

    # Try transit prekeys (asymmetric) - look up OWNER, not who knows about it
    # Hint is first KEY_ID_SIZE bytes of the full prekey_id, so we need prefix matching
    try:
        cursor = db._conn.execute(
            "SELECT DISTINCT connection_prekey_id, owner_peer_id FROM connection_prekeys"
        )
        recorded_by_peers = []
        prekey_count = 0
        for row in cursor.fetchall():
            prekey_id = row[0]
            prekey_count += 1
            try:
                prekey_id_bytes = crypto.b64decode(prekey_id)
                prekey_prefix = prekey_id_bytes[:crypto.KEY_ID_SIZE]
                if prekey_prefix == hint:
                    log.info(f"route_blob_to_peers: MATCH! prekey_id={prekey_id[:20]}... matches hint={hint_b64}")
                    recorded_by_peers.append(row[1])
            except Exception as e:
                log.warning(f"route_blob_to_peers: error decoding prekey {prekey_id[:20]}...: {e}")
                continue
        log.warning(f"route_blob_to_peers: checked {prekey_count} prekeys, found {len(recorded_by_peers)} matches for hint={hint_b64}")
        if recorded_by_peers:
            log.debug(f"route_blob_to_peers: routed to {len(recorded_by_peers)} peers via transit_prekey")
    except Exception as e:
        log.warning(f"route_blob_to_peers: Failed to query connection_prekeys: {e}")
        recorded_by_peers = []

    return recorded_by_peers


def store_packet_from_addr(event_id: str, recorded_by: str, from_addr: tuple[str, int] | None, db: Any) -> None:
    """Store packet source address in packet_metadata staging table.

    Called during receive to capture source address for connection projection.
    Connection projectors read from packet_metadata to populate from_addr_ip/from_addr_port.

    Args:
        event_id: Event ID this address is for
        recorded_by: Peer ID receiving the event
        from_addr: Source address as (ip, port) tuple, or None
        db: Database connection
    """
    if not from_addr:
        return

    safedb = create_safe_db(db, recorded_by=recorded_by)
    ip, port = from_addr
    safedb.execute("""
        INSERT OR REPLACE INTO packet_metadata (event_id, recorded_by, from_addr_ip, from_addr_port)
        VALUES (?, ?, ?, ?)
    """, (event_id, recorded_by, ip, port))
    log.debug(f"store_packet_from_addr: stored {ip}:{port} for {event_id[:20]}...")


def store_incoming(
    blob: bytes,
    from_addr: tuple[str, int] | None,
    t_ms: int,
    db: Any,
    skip_store: bool = False,
    skip_unwrap: bool = False,
) -> list[str]:
    """Unwrap transit blob, store event, create recorded events for all peers who can decrypt.

    This is the unified receive function that:
    1. Routes by transit key - finds peers who can decrypt
    2. Unwraps blob (tries each peer until success)
    3. Stores event blob (once) for non-ephemeral events
    4. Creates recorded events for each peer
    5. Stores from_addr metadata for projection

    Args:
        blob: Transit-wrapped blob
        from_addr: Source address as (ip, port) tuple, or None
        t_ms: Current timestamp
        db: Database connection

    Returns:
        List of recorded_ids (one per peer who can decrypt), or empty list if unwrap fails
    """
    from events.network import recorded

    if skip_unwrap:
        return []

    # Calculate hint for logging
    hint_b64 = crypto.b64encode(blob[:crypto.KEY_ID_SIZE]) if len(blob) >= crypto.KEY_ID_SIZE else "too_short"

    # Route to peers who can decrypt (device-wide lookup)
    recorded_by_peers = route_blob_to_peers(blob, db)

    if not recorded_by_peers:
        log.warning(f"store_incoming: no peers can decrypt blob (unknown key), hint_b64={hint_b64}")
        return []
    else:
        log.warning(f"store_incoming: found {len(recorded_by_peers)} peers for hint_b64={hint_b64}")

    # Try to unwrap with each peer who has access
    unwrapped_blob = None
    for peer_id in recorded_by_peers:
        log.warning(f"store_incoming: trying unwrap with peer_id={peer_id[:20]}...")
        unwrapped_blob, missing_keys = crypto.unwrap_transit(blob, peer_id, db)
        if unwrapped_blob is not None:
            log.warning(f"store_incoming: unwrap SUCCESS, blob size={len(unwrapped_blob)}")
            break
        else:
            log.warning(f"store_incoming: unwrap FAILED for peer_id={peer_id[:20]}..., missing_keys={missing_keys}")

    if unwrapped_blob is None:
        log.warning(f"store_incoming: unwrap failed for ALL {len(recorded_by_peers)} peers")
        return []

    log.warning(f"store_incoming: proceeding to store unwrapped blob size={len(unwrapped_blob)}")

    parsed = None
    is_file_slice = False
    if _write_behind_enabled():
        log.warning(f"store_incoming: write_behind enabled, buffering")
        parsed = _parse_unwrapped_json(unwrapped_blob)
        if parsed and _handle_negentropy_envelope(parsed, recorded_by_peers, t_ms, db):
            return []
        if parsed and parsed.get('type') == 'file_slice':
            is_file_slice = True

    if skip_store:
        log.warning(f"store_incoming: skip_store=True, returning empty")
        return []

    if _write_behind_enabled():
        _buffer_write_behind(unwrapped_blob, recorded_by_peers, from_addr, t_ms, is_file_slice)
        return []

    # Store the unwrapped event blob (once)
    event_id = store.blob(unwrapped_blob, t_ms, True, db)
    log.warning(f"store_incoming: stored event {event_id[:20]}..., creating recorded for {len(recorded_by_peers)} peers")

    # Create recorded event for EACH peer who can decrypt + store from_addr in staging
    recorded_ids = []
    for peer_id in recorded_by_peers:
        recorded_id = recorded.create(event_id, peer_id, t_ms, db, True)
        store_packet_from_addr(event_id, peer_id, from_addr, db)
        recorded_ids.append(recorded_id)
        log.warning(f"store_incoming: created recorded_id={recorded_id[:20]}... for peer_id={peer_id[:20]}...")

    log.warning(f"store_incoming: returning {len(recorded_ids)} recorded_ids")
    return recorded_ids


def unwrap_and_store(blob: bytes, t_ms: int, db: Any,
                     source_ip: str = None, source_port: int = None) -> dict:
    """Unwrap transit blob, store event, create recorded events for all peers with access.

    Edge case: If multiple local peers have the same key (e.g., two peers in the same network
    both accepted the same invite), this creates a separate recorded event for each peer.

    Args:
        blob: Transit-wrapped blob
        t_ms: Timestamp
        db: Database connection
        source_ip: Source IP address (for address learning)
        source_port: Source port (for address learning)

    Returns:
        Dict with 'recorded_ids', 'event_id', 'recorded_by_peers', or empty dict if unwrap fails
    """
    from events.network import recorded

    # Route to peers who can decrypt (device-wide lookup)
    recorded_by_peers = route_blob_to_peers(blob, db)

    if not recorded_by_peers:
        log.debug(f"unwrap_and_store: no peers can decrypt blob (unknown key)")
        return {'recorded_ids': [], 'event_id': None, 'recorded_by_peers': []}

    # Try to unwrap with each peer who has access (for prekeys, only owner can decrypt)
    unwrapped_blob = None
    for peer_id in recorded_by_peers:
        unwrapped_blob, missing_keys = crypto.unwrap_transit(blob, peer_id, db)
        if unwrapped_blob is not None:
            break

    if unwrapped_blob is None:
        log.debug(f"unwrap_and_store: unwrap failed for {len(recorded_by_peers)} peers")
        return {'recorded_ids': [], 'event_id': None, 'recorded_by_peers': []}

    # Store the unwrapped event blob (once)
    # All events go through normal storage and projection (no ephemeral bypass)
    event_id = store.blob(unwrapped_blob, t_ms, True, db)
    log.debug(f"unwrap_and_store: stored event {event_id[:20]}..., creating recorded for {len(recorded_by_peers)} peers")

    # Create recorded event for EACH peer who can decrypt
    recorded_ids = []
    for recorded_by in recorded_by_peers:
        recorded_id = recorded.create(event_id, recorded_by, t_ms, db, True)
        recorded_ids.append(recorded_id)

    return {
        'recorded_ids': recorded_ids,
        'event_id': event_id,
        'recorded_by_peers': recorded_by_peers,
    }


def _update_connection_request_addresses(event_source_addresses: dict, t_ms: int, db: Any) -> None:
    """Update pending_connection_requests with source addresses from packets.

    Called after projection to populate source_ip/source_port for connection_request events.
    This enables the ack to be sent back to the correct address.

    Args:
        event_source_addresses: Dict of event_id -> (source_ip, source_port, recorded_by_peers)
        t_ms: Current timestamp
        db: Database connection
    """
    for event_id, (source_ip, source_port, recorded_by_peers) in event_source_addresses.items():
        # Check if this event is a connection_request (by checking if it's in pending_connection_requests)
        # Update for each recorded_by peer
        for peer_id in recorded_by_peers:
            safedb = create_safe_db(db, recorded_by=peer_id)
            # Update if exists (the request was projected into pending_connection_requests)
            safedb.execute("""
                UPDATE pending_connection_requests
                SET source_ip = ?, source_port = ?
                WHERE request_id = ? AND recorded_by = ?
            """, (source_ip, source_port, event_id, peer_id))

            # Also update connections table if this is an established connection
            safedb.execute("""
                UPDATE connections
                SET peer_ip = ?, peer_port = ?, address_source = 'packet', address_learned_ms = ?
                WHERE connection_id = ? AND recorded_by = ? AND peer_ip IS NULL
            """, (source_ip, source_port, t_ms, event_id, peer_id))

    log.debug(f"_update_connection_request_addresses: updated {len(event_source_addresses)} events")


def _process_address_observations(transit_packets: list[dict], t_ms: int, db: Any) -> None:
    """Try to observe source peers from transit packets (NAT integration).

    This is an optional integration point - if the network layer is initialized,
    we can use it to create address events. Otherwise, this is a no-op.
    """
    try:
        from simulator import network

        engine = network.get_engine()
        if not engine or not engine.peer_endpoints:
            # Network engine not initialized, skip observations
            return

        # Count packets with source addresses for logging
        packets_with_addr = sum(1 for p in transit_packets if p.get('source_ip'))
        log.debug(f"_process_address_observations: network layer available, {len(transit_packets)} packets ({packets_with_addr} with addresses)")

    except ImportError:
        # Network layer not available
        pass
    except Exception as e:
        log.debug(f"_process_address_observations: error: {e}")


def _batch_add_negentropy_for_recorded(t_ms: int, db: Any) -> None:
    """Batch-add shareable events for this tick to negentropy with incremental buckets."""
    from events.network import negentropy

    # Query shareable_events for events just added (recorded_at = t_ms)
    # Use raw connection to bypass scoping (we need to query across all peers)
    cursor = db._conn.execute(
        "SELECT event_id, can_share_peer_id, recorded_at FROM shareable_events WHERE recorded_at = ?",
        (t_ms,)
    )
    new_shareable = [{'event_id': r[0], 'can_share_peer_id': r[1], 'recorded_at': r[2]} for r in cursor.fetchall()]

    if not new_shareable:
        return

    # Group by peer_id for batch processing
    by_peer: dict[str, list[tuple[str, int]]] = {}
    for row in new_shareable:
        peer_id = row['can_share_peer_id']
        if peer_id not in by_peer:
            by_peer[peer_id] = []
        by_peer[peer_id].append((row['event_id'], row['recorded_at']))

    if os.getenv("SYNC_PERF_SKIP_BUCKETS") == "1":
        for peer_id, events in by_peer.items():
            negentropy.add_events_to_sync_batch(db, peer_id, events, defer_buckets=True)
            log.debug(f"receive: batch-added {len(events)} events to negentropy for peer {peer_id[:20]}... (buckets skipped)")
        return

    # Batch-add to negentropy for each peer with incremental bucket updates
    for peer_id, events in by_peer.items():
        negentropy.add_events_to_sync_batch(db, peer_id, events, incremental_buckets=True)
        log.debug(f"receive: batch-added {len(events)} events to negentropy for peer {peer_id[:20]}...")


def receive(batch_size: int, t_ms: int, db: Any) -> None:
    """Receive and process a batch of incoming transit blobs."""
    from events.network import recorded

    transit_packets = queues.incoming.drain(batch_size, t_ms, db)
    log.info(f"receive: processing {len(transit_packets)} blobs")

    # unwrap_and_store returns dict with recorded_ids, event_id, recorded_by_peers
    # Track source addresses for address learning after projection
    unwrap_results = []
    event_source_addresses = {}  # event_id -> (source_ip, source_port, recorded_by_peers)

    for packet in transit_packets:
        blob = packet['blob']
        source_ip = packet.get('source_ip')
        source_port = packet.get('source_port')
        result = unwrap_and_store(blob, t_ms, db, source_ip=source_ip, source_port=source_port)
        unwrap_results.append(result)

        # Track source address for this event (for address learning after projection)
        if result.get('event_id') and source_ip and source_port:
            event_source_addresses[result['event_id']] = (source_ip, source_port, result['recorded_by_peers'])

    # Flatten and project all recorded events
    # Skip negentropy updates during projection - we'll batch them after
    valid_recorded_ids = [id for r in unwrap_results for id in r.get('recorded_ids', [])]
    log.debug(f"receive: projecting {len(valid_recorded_ids)} recorded events (skip_negentropy=True)")

    recorded.project_ids(valid_recorded_ids, db, skip_negentropy=True)

    # Address learning: update pending_connection_requests with source addresses
    # This happens after projection so the requests are in the table
    if event_source_addresses:
        _update_connection_request_addresses(event_source_addresses, t_ms, db)
        log.debug(f"receive: processed {len(event_source_addresses)} events for address learning")

    _batch_add_negentropy_for_recorded(t_ms, db)

    # Try to integrate with network layer for address observations (optional)
    try:
        _process_address_observations(transit_packets, t_ms, db)
    except Exception as e:
        log.debug(f"receive: address observation integration not fully ready: {e}")

    db.commit()
