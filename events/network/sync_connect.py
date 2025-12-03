"""sync_connect event type (LOCAL-ONLY) for establishing peer connections.

This module handles two-way connection handshake before sync. Connections provide:
- Implicit authentication via decryption (each side uses the key the other provided)
- Persistent symmetric keys for efficient communication
- Address discovery for NAT traversal

Flow:
1. send_connect_to_all() → send_connect() for each known peer
2. Connect event contains our transit_key, wrapped with recipient's transit_prekey
3. Recipient's project() validates, stores connection, and sends sync_connect_ack back
4. Ack contains their transit_key (wrapped to our key), allowing bidirectional sync
5. Sync uses established connections with both peers' transit keys
"""
from typing import Any

# Registry metadata
EVENT_TYPE = 'sync_connect'
SHAREABLE = False  # Local-only - connection state is per-peer
EPHEMERAL = True   # Drop if deps missing - sender will retry
PROJECTION_TABLE = None
import logging
import json
import crypto
import store
from events.identity import peer
from events.network import transit_key, transit_prekey
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

    log.info(f"sync_connect.send_connect_to_all: found {len(local_peer_rows)} local peers at t_ms={t_ms}")
    log.debug(f"sync_connect: sending from {len(local_peer_rows)} local peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']

        # Find this peer's peer_shared_id from peer_self table
        peer_shared_id = None
        safedb = create_safe_db(db, recorded_by=peer_id)
        peer_self_row = safedb.query_one(
            "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
            (peer_id, peer_id)
        )

        if peer_self_row:
            peer_shared_id = peer_self_row['peer_shared_id']

        if not peer_shared_id:
            log.debug(f"sync_connect: skipping peer {peer_id[:20]}... (no peer_shared_id)")
            continue

        log.warning(f"[SYNC_CONNECT_PEER_ID] peer={peer_id[:10]}... peer_shared_id={peer_shared_id[:10]}...")

        # Send connects to all known peers from FOUR sources:
        # 1. Synced peers (discovered via normal sync) - from peers_shared table
        # 2. Bootstrap peers (from invite/link acceptance) - from invite_accepteds table
        # 3. Connected peers (received sync_connect from them) - from sync_connections table
        # 4. Linked peers (devices linked to same user) - from peers_shared table where user_id = our user_id
        # This unified query enables connections before sync completes (e.g., initial join, device linking)

        # Query synced peers
        peer_shared_rows = safedb.query(
            "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
            (peer_id,)
        )
        peers_shared_ids = [row['peer_shared_id'] for row in peer_shared_rows]
        log.warning(f"[SYNC_CONNECT_PEERS_SHARED] peer={peer_id[:10]}... peers_shared={len(peer_shared_rows)} ids={[pid[:10]+'...' if pid else 'NULL' for pid in peers_shared_ids]}")

        # Query bootstrap peers (inviter from invite/link acceptance)
        bootstrap_rows = safedb.query(
            "SELECT inviter_peer_shared_id as peer_shared_id FROM invite_accepteds WHERE recorded_by = ?",
            (peer_id,)
        )
        bootstrap_peer_ids = [row['peer_shared_id'] for row in bootstrap_rows]
        log.warning(f"[SYNC_CONNECT_BOOTSTRAP] peer={peer_id[:10]}... bootstrap_rows={len(bootstrap_rows)} ids={[pid[:10]+'...' if pid else 'NULL' for pid in bootstrap_peer_ids]}")

        # Query connected peers (peers who have sent us sync_connect)
        # Only include active connections (not expired)
        connection_rows = unsafedb.query(
            "SELECT peer_shared_id FROM sync_connections WHERE last_seen_ms + ttl_ms > ?",
            (t_ms,)
        )

        # Query linked peers (other devices of same user - get our user_id then find all peers with that user_id)
        our_user_id = None
        our_peer_row = safedb.query_one(
            "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
            (peer_shared_id, peer_id)
        )
        if our_peer_row and our_peer_row['user_id']:
            our_user_id = our_peer_row['user_id']

        linked_rows = []
        if our_user_id:
            linked_rows = safedb.query(
                "SELECT peer_shared_id FROM peers_shared WHERE user_id = ? AND recorded_by = ? AND peer_shared_id != ?",
                (our_user_id, peer_id, peer_shared_id)
            )
        else:
            log.warning(f"[SYNC_CONNECT_LINKED_PEERS] No user_id found for our peer, skipping linked peers discovery")

        # Combine and deduplicate all peer IDs
        all_peer_ids = set()
        for row in peer_shared_rows:
            all_peer_ids.add(row['peer_shared_id'])
        for row in bootstrap_rows:
            all_peer_ids.add(row['peer_shared_id'])
        for row in connection_rows:
            all_peer_ids.add(row['peer_shared_id'])
        for row in linked_rows:
            all_peer_ids.add(row['peer_shared_id'])

        all_peer_ids_list = [pid[:10] + '...' for pid in all_peer_ids]
        log.warning(f"[SYNC_CONNECT_DISCOVERY] peer={peer_id[:10]}... self={peer_shared_id[:10]}... total_peers={len(all_peer_ids)} peers_shared={len(peer_shared_rows)} bootstrap={len(bootstrap_rows)} connections={len(connection_rows)} linked={len(linked_rows)} all_ids={all_peer_ids_list}")

        # Get invite_id if this peer used an invite to join
        invite_row = safedb.query_one(
            "SELECT invite_id FROM invite_accepteds WHERE recorded_by = ? LIMIT 1",
            (peer_id,)
        )
        invite_id = invite_row['invite_id'] if invite_row else None

        # Send connect to each known peer
        for to_peer_shared_id in all_peer_ids:
            log.warning(f"[SYNC_CONNECT_LOOP] peer={peer_id[:10]}... considering_peer={to_peer_shared_id[:10]}...")

            # Skip self
            if to_peer_shared_id == peer_shared_id:
                log.warning(f"[SYNC_CONNECT_SKIP_SELF] peer={peer_id[:10]}... skipping_self={to_peer_shared_id[:10]}...")
                continue

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
                log.warning(f"[SYNC_CONNECT_EXCEPTION] peer={peer_id[:10]}... to_peer={to_peer_shared_id[:10]}... error={e}")


def send_connect(to_peer_shared_id: str, from_peer_id: str, from_peer_shared_id: str,
                 invite_id: str | None, t_ms: int, db: Any) -> None:
    """Send a connection announcement to a specific peer.

    Args:
        to_peer_shared_id: Recipient's public peer identity
        from_peer_id: Sender's local peer ID
        from_peer_shared_id: Sender's public peer identity
        invite_id: Optional invite ID for authentication (not used in new protocol, kept for compatibility)
        t_ms: Current timestamp
        db: Database connection
    """
    log.debug(f"sync_connect: sending from {from_peer_shared_id[:20]}... to {to_peer_shared_id[:20]}...")

    # Create transit key (symmetric key for them to send to us)
    transit_key_id = transit_key.create(from_peer_id, t_ms, db)
    transit_key_dict = transit_key.get_key(transit_key_id, from_peer_id, db)
    transit_key_bytes = transit_key_dict.get('key') if transit_key_dict else None

    if not transit_key_bytes:
        log.warning(f"[SYNC_CONNECT_NO_TRANSIT_KEY] from={from_peer_shared_id[:10]}... to={to_peer_shared_id[:10]}...")
        return

    # Build connect event data
    connect_data = {
        'type': 'sync_connect',
        'transit_key_id': transit_key_id,
        'transit_key': crypto.b64encode(transit_key_bytes),
        'signed_by': from_peer_shared_id,
        'invite_id': invite_id,  # Include if sender is a joiner (None otherwise)
        'created_at': t_ms,
        'ttl_ms': 300000  # 5 minutes
    }

    # Sign with peer's private key
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
    signed_connect = crypto.sign_event(connect_data, private_key)

    # If invite_id present, also sign with invite private key for bootstrap auth
    # This allows inviter to verify joiner before peer_shared is synced
    if invite_id:
        safedb = create_safe_db(db, recorded_by=from_peer_id)

        # Look up invite private key from group_prekeys
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

    # Try to get established connection first (uses symmetric transit key)
    # This allows bidirectional sync_connect even before transit_prekey_shared syncs
    unsafedb = create_unsafe_db(db)
    conn = unsafedb.query_one("""
        SELECT their_transit_key_id, their_transit_key
        FROM sync_connections
        WHERE peer_shared_id = ?
          AND last_seen_ms + ttl_ms > ?
    """, (to_peer_shared_id, t_ms))

    if conn:
        # Use established connection's transit key (symmetric)
        to_key = {
            'id': crypto.b64decode(conn['their_transit_key_id']),
            'key': conn['their_transit_key'],
            'type': 'symmetric'
        }
        log.info(f"sync_connect: using established connection with {to_peer_shared_id[:20]}...")
    else:
        # Fall back to transit prekey (asymmetric)
        to_key = transit_prekey.get_transit_prekey_for_peer(to_peer_shared_id, from_peer_id, db)
        if not to_key:
            log.warning(f"[SYNC_CONNECT_NO_PREKEY] from={from_peer_shared_id[:10]}... to={to_peer_shared_id[:10]}... CANNOT_SEND")
            return

    wrapped = crypto.wrap(canonical, to_key, db)

    # Queue for delivery
    import queues
    queues.incoming.add(wrapped, t_ms, db)

    log.warning(f"[SYNC_CONNECT_SEND] from={from_peer_shared_id[:10]}... to={to_peer_shared_id[:10]}...")


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project sync_connect event: validate, store connection, and send ack.

    Validates peer signature (implicit auth via decryption),
    stores connection with sender's transit_key,
    and sends sync_connect_ack back with our transit_key.

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
        return None

    event_data = crypto.parse_json(blob)

    # ENFORCEMENT: Reject connections from removed peers
    peer_shared_id = event_data.get('signed_by')
    if peer_shared_id:
        removed_check = unsafedb.query_one(
            "SELECT 1 FROM removed_peers WHERE peer_shared_id = ? LIMIT 1",
            (peer_shared_id,)
        )
        if removed_check:
            log.info(f"sync_connect.project(): rejecting connection from removed peer {peer_shared_id[:20]}...")
            return None

    # Authentication: Try invite signature first (for bootstrap), fall back to peer signature
    invite_id = event_data.get('invite_id')
    invite_signature_b64 = event_data.get('invite_signature')
    authenticated = False

    # Try invite signature first (allows joiner to connect before peer_shared syncs)
    if invite_id and invite_signature_b64:
        invite_blob = store.get(invite_id, unsafedb)
        if invite_blob:
            try:
                invite_event = crypto.parse_json(invite_blob)
                invite_public_key = crypto.b64decode(invite_event.get('invite_pubkey', ''))

                # Verify signature over the signed_connect structure (without invite_signature field)
                connect_without_invite_sig = {k: v for k, v in event_data.items() if k != 'invite_signature'}
                sig_data = json.dumps(connect_without_invite_sig, sort_keys=True).encode()
                invite_signature = crypto.b64decode(invite_signature_b64)

                if crypto.verify(sig_data, invite_signature, invite_public_key):
                    log.info(f"sync_connect.project: ✓ invite signature verified for {invite_id[:20]}...")
                    authenticated = True
            except Exception as e:
                log.debug(f"sync_connect.project: invite signature check failed: {e}")

    # Fall back to peer signature verification
    if not authenticated:
        if crypto.verify_signed_by_peer_shared(event_data, recorded_by, db):
            log.debug(f"sync_connect.project: ✓ peer signature verified")
            authenticated = True

    if not authenticated:
        log.warning(f"sync_connect.project: authentication failed (no valid invite or peer signature)")
        return None

    # Extract connection info
    transit_key_id = event_data.get('transit_key_id')
    transit_key_b64 = event_data.get('transit_key')
    transit_key_bytes = crypto.b64decode(transit_key_b64) if transit_key_b64 else None

    if not peer_shared_id or not transit_key_id or not transit_key_bytes:
        log.warning(f"sync_connect.project: missing required fields (peer_shared_id={bool(peer_shared_id)}, transit_key_id={bool(transit_key_id)}, transit_key={bool(transit_key_bytes)})")
        return None

    # Upsert into sync_connections table with THEIR transit_key
    unsafedb.execute("""
        INSERT OR REPLACE INTO sync_connections
        (peer_shared_id, their_transit_key_id, their_transit_key, last_seen_ms, ttl_ms)
        VALUES (?, ?, ?, ?, ?)
    """, (
        peer_shared_id,
        transit_key_id,
        transit_key_bytes,
        recorded_at,
        300000  # 5 minutes default TTL
    ))

    log.warning(f"[SYNC_CONNECT_RECEIVED] from={peer_shared_id[:10]}... recorded_by={recorded_by[:10]}... STORING_CONNECTION")

    # Send sync_connect_ack back
    try:
        send_connect_ack(
            to_peer_shared_id=peer_shared_id,
            from_peer_id=recorded_by,
            t_ms=recorded_at,
            db=db
        )
    except Exception as e:
        log.warning(f"sync_connect.project: failed to send ack to {peer_shared_id[:10]}...: {e}")

    return event_id


def send_connect_ack(to_peer_shared_id: str, from_peer_id: str, t_ms: int, db: Any) -> None:
    """Send connection acknowledgement with our transit_key, wrapped to their key.

    Args:
        to_peer_shared_id: Recipient's public peer identity
        from_peer_id: Sender's local peer ID
        t_ms: Current timestamp
        db: Database connection
    """
    log.debug(f"sync_connect_ack: sending from {from_peer_id[:20]}... to {to_peer_shared_id[:20]}...")

    # Get our peer_shared_id
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (from_peer_id, from_peer_id)
    )
    if not peer_self_row:
        log.warning(f"[SYNC_CONNECT_ACK_NO_PEER_SELF] from={from_peer_id[:10]}...")
        return

    from_peer_shared_id = peer_self_row['peer_shared_id']

    # Create our transit key (symmetric key for them to send to us in future connects)
    transit_key_id = transit_key.create(from_peer_id, t_ms, db)
    transit_key_dict = transit_key.get_key(transit_key_id, from_peer_id, db)
    transit_key_bytes = transit_key_dict.get('key') if transit_key_dict else None

    if not transit_key_bytes:
        log.warning(f"[SYNC_CONNECT_ACK_NO_KEY] from={from_peer_id[:10]}... to={to_peer_shared_id[:10]}...")
        return

    # Build ack event data (no signature - implicit auth via decryption)
    # Include from_peer_shared_id so recipient can match ack to connection
    ack_data = {
        'type': 'sync_connect_ack',
        'from_peer_shared_id': from_peer_shared_id,  # Who is sending the ack
        'transit_key_id': transit_key_id,
        'transit_key': crypto.b64encode(transit_key_bytes),
        'created_at': t_ms,
        'ttl_ms': 300000  # 5 minutes
    }

    # Canonicalize to JSON
    canonical = crypto.canonicalize_json(ack_data)

    # Get their transit_key from the connection we just stored
    unsafedb = create_unsafe_db(db)
    conn = unsafedb.query_one("""
        SELECT their_transit_key_id, their_transit_key
        FROM sync_connections
        WHERE peer_shared_id = ?
    """, (to_peer_shared_id,))

    if not conn or not conn['their_transit_key']:
        log.warning(f"[SYNC_CONNECT_ACK_NO_CONNECTION] from={from_peer_id[:10]}... to={to_peer_shared_id[:10]}...")
        return

    # Wrap ack with their transit_key (the key they sent us in the connect)
    to_key = {
        'id': crypto.b64decode(conn['their_transit_key_id']),
        'key': conn['their_transit_key'],
        'type': 'symmetric'
    }

    wrapped = crypto.wrap(canonical, to_key, db)

    # Queue for delivery
    import queues
    queues.incoming.add(wrapped, t_ms, db)

    log.warning(f"[SYNC_CONNECT_ACK_SEND] from={from_peer_id[:10]}... to={to_peer_shared_id[:10]}...")


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
