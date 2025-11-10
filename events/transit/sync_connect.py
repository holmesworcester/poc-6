"""sync_connect event type (LOCAL-ONLY) for establishing peer connections.

This module handles connection establishment before sync. Connections provide:
- Explicit authentication (peer signature + optional invite signature)
- Persistent symmetric keys for efficient communication
- Address discovery for NAT traversal

Flow:
1. send_connect_to_all() → send_connect() for each known peer
2. Connect event wrapped with recipient's transit_prekey
3. Recipient's project() validates and stores in sync_connections table
4. Sync uses established connections instead of looking up prekeys each time
"""
from typing import Any
import logging
import json
import crypto
import store
from events.identity import peer
from events.transit import transit_key, transit_prekey
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def send_connect_to_all(t_ms: int, db: Any) -> None:
    """Send connection announcements from all local peers to all known peers.

    This establishes or refreshes connections before sync operations.
    Called by tick() on every cycle.

    Args:
        t_ms: Current timestamp in milliseconds
        db: Database connection
    """
    # Query all local peers
    unsafedb = create_unsafe_db(db)
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")

    log.debug(f"sync_connect: sending from {len(local_peer_rows)} local peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']

        # Find this peer's peer_shared_id
        peer_shared_id = None
        safedb = create_safe_db(db, recorded_by=peer_id)
        candidate_rows = safedb.query(
            "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
            (peer_id,)
        )
        for row in candidate_rows:
            ps_id = row['peer_shared_id']
            try:
                ps_blob = store.get(ps_id, db)
                if not ps_blob:
                    continue
                ps_data = crypto.parse_json(ps_blob)
                if ps_data.get('type') == 'peer_shared' and ps_data.get('peer_id') == peer_id:
                    peer_shared_id = ps_id
                    break
            except Exception:
                continue

        if not peer_shared_id:
            log.debug(f"sync_connect: skipping peer {peer_id[:20]}... (no peer_shared_id)")
            continue

        # Send connects to all known peers (from peers_shared table)
        peer_shared_rows = safedb.query(
            "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
            (peer_id,)
        )

        for row in peer_shared_rows:
            to_peer_shared_id = row['peer_shared_id']

            # Skip self
            if to_peer_shared_id == peer_shared_id:
                continue

            # Get invite_id if this peer used an invite to join
            invite_row = safedb.query_one(
                "SELECT invite_id FROM invite_accepteds WHERE recorded_by = ? LIMIT 1",
                (peer_id,)
            )
            invite_id = invite_row['invite_id'] if invite_row else None

            try:
                send_connect(
                    to_peer_shared_id=to_peer_shared_id,
                    from_peer_id=peer_id,
                    from_peer_shared_id=peer_shared_id,
                    invite_id=invite_id,
                    t_ms=t_ms,
                    db=db
                )
            except Exception as e:
                log.warning(f"sync_connect: failed to send to {to_peer_shared_id[:20]}...: {e}")


def send_connect(to_peer_shared_id: str, from_peer_id: str, from_peer_shared_id: str,
                 invite_id: str | None, t_ms: int, db: Any) -> None:
    """Send a connection announcement to a specific peer.

    Args:
        to_peer_shared_id: Recipient's public peer identity
        from_peer_id: Sender's local peer ID
        from_peer_shared_id: Sender's public peer identity
        invite_id: Optional invite ID for authentication
        t_ms: Current timestamp
        db: Database connection
    """
    log.debug(f"sync_connect: sending from {from_peer_shared_id[:20]}... to {to_peer_shared_id[:20]}...")

    # Create response transit key (symmetric key for replies)
    response_transit_key_id = transit_key.create(from_peer_id, t_ms, db)
    response_transit_key_dict = transit_key.get_key(response_transit_key_id, from_peer_id, db)
    response_transit_key_bytes = response_transit_key_dict.get('key') if response_transit_key_dict else None

    if not response_transit_key_bytes:
        log.error(f"sync_connect: failed to create response_transit_key")
        return

    # Build connect event data
    connect_data = {
        'type': 'sync_connect',
        'peer_id': from_peer_id,
        'created_by': from_peer_shared_id,
        'address': '127.0.0.1',  # TODO: get from network layer
        'port': 8000,  # TODO: get from network layer
        'response_transit_key_id': response_transit_key_id,
        'response_transit_key': crypto.b64encode(response_transit_key_bytes),
        'invite_id': invite_id,  # Always include (None if not a joiner)
        'created_at': t_ms
    }

    # Sign with peer's private key
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
    signed_connect = crypto.sign_event(connect_data, private_key)

    # If invite_id present, also sign with invite private key
    # The invite private key is stored in group_prekeys when invite_accepted is projected
    # We can find it by looking for a prekey owned by this peer (joiners have their invite key stored there)
    if invite_id:
        safedb = create_safe_db(db, recorded_by=from_peer_id)

        # Look up invite private key from group_prekeys
        # The invite_prekey_id is stored when invite_accepted was projected
        invite_key_row = safedb.query_one(
            "SELECT private_key FROM group_prekeys WHERE owner_peer_id = ? AND recorded_by = ? LIMIT 1",
            (from_peer_id, from_peer_id)
        )

        if invite_key_row:
            invite_private_key = invite_key_row['private_key']
            # Create invite signature over the entire signed_connect structure
            invite_sig_data = json.dumps(signed_connect, sort_keys=True).encode()
            invite_signature = crypto.sign(invite_sig_data, invite_private_key)
            signed_connect['invite_signature'] = crypto.b64encode(invite_signature)
            log.debug(f"sync_connect: added invite_signature for invite {invite_id[:20]}...")

    # Canonicalize to JSON
    canonical = crypto.canonicalize_json(signed_connect)

    # Wrap with recipient's transit prekey
    to_key = transit_prekey.get_transit_prekey_for_peer(to_peer_shared_id, from_peer_id, db)
    if not to_key:
        log.warning(f"sync_connect: no transit_prekey for {to_peer_shared_id[:20]}..., cannot send")
        return

    wrapped = crypto.wrap(canonical, to_key, db)

    # Queue for delivery
    import queues
    queues.incoming.add(wrapped, t_ms, db)

    log.info(f"sync_connect: sent connect from {from_peer_shared_id[:20]}... to {to_peer_shared_id[:20]}...")


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Project sync_connect event: validate and store connection info.

    Validates both peer signature and optional invite signature,
    then stores connection in sync_connections table.

    Args:
        event_id: The sync_connect event ID
        recorded_by: Local peer who received this connect
        recorded_at: When received
        db: Database connection
    """
    log.debug(f"sync_connect.project: event_id={event_id[:20]}... recorded_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"sync_connect.project: blob not found")
        return

    event_data = crypto.parse_json(blob)

    # Verify peer signature
    try:
        crypto.verify_signed_by_peer_shared(event_data, recorded_by, db)
    except Exception as e:
        log.warning(f"sync_connect.project: peer signature verification failed: {e}")
        return

    # Verify invite signature if present
    if event_data.get('invite_id') and event_data.get('invite_signature'):
        invite_id = event_data['invite_id']
        invite_signature_b64 = event_data['invite_signature']

        # Get invite public key
        invite_blob = store.get(invite_id, unsafedb)
        if invite_blob:
            try:
                invite_event = crypto.parse_json(invite_blob)
                invite_public_key = crypto.b64decode(invite_event.get('invite_pubkey', ''))

                # Verify signature over the signed_connect structure (without invite_signature field)
                connect_without_sig = {k: v for k, v in event_data.items() if k != 'invite_signature'}
                sig_data = json.dumps(connect_without_sig, sort_keys=True).encode()
                invite_signature = crypto.b64decode(invite_signature_b64)

                if not crypto.verify(sig_data, invite_signature, invite_public_key):
                    log.warning(f"sync_connect.project: invite signature verification failed")
                    return

                log.debug(f"sync_connect.project: invite signature verified for {invite_id[:20]}...")
            except Exception as e:
                log.warning(f"sync_connect.project: invite signature check failed: {e}")
                # Continue anyway - invite auth is opportunistic

    # Extract connection info
    peer_shared_id = event_data.get('created_by')
    response_transit_key_id = event_data.get('response_transit_key_id')
    response_transit_key = crypto.b64decode(event_data.get('response_transit_key', ''))
    address = event_data.get('address')
    port = event_data.get('port')
    invite_id = event_data.get('invite_id')

    if not all([peer_shared_id, response_transit_key_id, response_transit_key]):
        log.warning(f"sync_connect.project: missing required fields")
        return

    # Upsert into sync_connections table (device-wide, no recorded_by)
    unsafedb.execute("""
        INSERT OR REPLACE INTO sync_connections
        (peer_shared_id, response_transit_key_id, response_transit_key,
         address, port, invite_id, last_seen_ms, ttl_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        peer_shared_id,
        response_transit_key_id,
        response_transit_key,
        address,
        port,
        invite_id,
        recorded_at,
        300000  # 5 minutes default TTL
    ))

    # Mark as valid
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (event_id, recorded_by)
    )

    log.info(f"sync_connect.project: established connection with {peer_shared_id[:20]}...")


def purge_expired(t_ms: int, db: Any) -> None:
    """Remove expired connections from sync_connections table.

    Connections expire when: last_seen_ms + ttl_ms < current_time
    Called by tick() periodically.

    Args:
        t_ms: Current timestamp in milliseconds
        db: Database connection
    """
    unsafedb = create_unsafe_db(db)

    # Delete expired connections
    result = unsafedb.execute("""
        DELETE FROM sync_connections
        WHERE last_seen_ms + ttl_ms < ?
    """, (t_ms,))

    deleted_count = result.rowcount if hasattr(result, 'rowcount') else 0

    if deleted_count > 0:
        log.info(f"sync_connect: purged {deleted_count} expired connections")
