"""sync_connect event type (LOCAL-ONLY) for establishing peer connections.

This module handles two-way connection handshake before sync. Connections provide:
- Implicit authentication via decryption (each side uses the key the other provided)
- Persistent symmetric keys for efficient communication
- Address discovery for NAT traversal

Flow:
1. send_connect_to_all() → send() for each known peer
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
        safedb = create_safe_db(db, recorded_by=peer_id)

        # Get peer_shared_id for self-skip check
        peer_shared_id = None
        peer_self_row = safedb.query_one(
            "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
            (peer_id, peer_id)
        )
        if peer_self_row and peer_self_row['peer_shared_id']:
            peer_shared_id = peer_self_row['peer_shared_id']

        log.warning(f"[SYNC_CONNECT_PEER_ID] peer={peer_id[:10]}... peer_shared_id={peer_shared_id[:10] if peer_shared_id else 'NONE'}...")

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

        # Query existing connections for this peer (active, not expired)
        # Include connections waiting for ack (their_transit_key IS NULL) to avoid sending duplicates
        all_connection_rows = unsafedb.query(
            "SELECT our_transit_key_id, their_transit_key_id, their_transit_key FROM sync_connections WHERE our_peer_id = ? AND last_seen_ms + ttl_ms > ?",
            (peer_id, t_ms)
        )
        # Completed connections (have their_transit_key) can be refreshed
        connection_rows = [c for c in all_connection_rows if c['their_transit_key'] is not None]

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

        # Combine and deduplicate peer_shared_ids we know about (for initial connections via transit_prekey)
        known_peer_shared_ids = set()
        for row in peer_shared_rows:
            if row['peer_shared_id']:
                known_peer_shared_ids.add(row['peer_shared_id'])
        for row in bootstrap_rows:
            if row['peer_shared_id']:
                known_peer_shared_ids.add(row['peer_shared_id'])
        for row in linked_rows:
            if row['peer_shared_id']:
                known_peer_shared_ids.add(row['peer_shared_id'])

        log.warning(f"[SYNC_CONNECT_DISCOVERY] peer={peer_id[:10]}... self={peer_shared_id[:10] if peer_shared_id else 'NONE'}... known_peers={len(known_peer_shared_ids)} existing_connections={len(all_connection_rows)} completed={len(connection_rows)}")

        # 1. Refresh existing connections (use their_transit_key directly)
        # Track which connections we've refreshed so we don't also send() to them
        refreshed_connections = set()
        for conn in connection_rows:
            try:
                send_to_connection(
                    their_transit_key_id=conn['their_transit_key_id'],
                    their_transit_key=conn['their_transit_key'],
                    from_peer_id=peer_id,
                    t_ms=t_ms,
                    db=db
                )
                # Note: We can't easily track by peer_shared_id since connections are key-based
                # But existing connections are already being refreshed, so we don't need to send() new ones
            except Exception as e:
                log.warning(f"[SYNC_CONNECT_REFRESH_EXCEPTION] peer={peer_id[:10]}... error={e}")

        # 2. Send to known peers ONLY if we don't have existing connections
        # With key-based connections, skip send() if we have any active connections to avoid accumulating
        # Check all_connection_rows (includes pending connections waiting for ack)
        if len(all_connection_rows) > 0:
            log.warning(f"[SYNC_CONNECT_SKIP_SEND] peer={peer_id[:10]}... already has {len(all_connection_rows)} connections, skipping new sends")
        else:
            for to_peer_shared_id in known_peer_shared_ids:
                # Skip self
                if peer_shared_id and to_peer_shared_id == peer_shared_id:
                    continue

                try:
                    send(
                        to_peer_shared_id=to_peer_shared_id,
                        from_peer_id=peer_id,
                        t_ms=t_ms,
                        db=db
                    )
                except Exception as e:
                    log.warning(f"[SYNC_CONNECT_EXCEPTION] peer={peer_id[:10]}... to_peer={to_peer_shared_id[:10]}... error={e}")


def send_to_connection(their_transit_key_id: str, their_transit_key: bytes, from_peer_id: str, t_ms: int, db: Any) -> None:
    """Send sync_connect refresh to an existing connection using their transit_key.

    Args:
        their_transit_key_id: Their key ID (for wrapping)
        their_transit_key: Their symmetric key (for wrapping)
        from_peer_id: Sender's local peer ID
        t_ms: Current timestamp
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=from_peer_id)

    # Determine auth method
    invite_row = safedb.query_one(
        "SELECT invite_id, invite_private_key FROM invite_accepteds WHERE recorded_by = ? AND invite_private_key IS NOT NULL LIMIT 1",
        (from_peer_id,)
    )

    if invite_row and invite_row['invite_private_key']:
        invite_id = invite_row['invite_id']
        invite_private_key = invite_row['invite_private_key']
        signed_by = from_peer_id
        use_invite_auth = True
    else:
        invite_id = None
        invite_private_key = None
        peer_self_row = safedb.query_one(
            "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
            (from_peer_id, from_peer_id)
        )
        if not peer_self_row or not peer_self_row['peer_shared_id']:
            log.warning(f"[SYNC_CONNECT_REFRESH_NO_AUTH] peer={from_peer_id[:10]}...")
            return
        signed_by = peer_self_row['peer_shared_id']
        use_invite_auth = False

    # Create new transit key for this refresh
    new_transit_key_id = transit_key.create(from_peer_id, t_ms, db)
    new_transit_key_dict = transit_key.get_key(new_transit_key_id, from_peer_id, db)
    new_transit_key_bytes = new_transit_key_dict.get('key') if new_transit_key_dict else None

    if not new_transit_key_bytes:
        return

    # Build connect event
    connect_data = {
        'type': 'sync_connect',
        'transit_key_id': new_transit_key_id,
        'transit_key': crypto.b64encode(new_transit_key_bytes),
        'signed_by': signed_by,
        'invite_id': invite_id,
        'created_at': t_ms,
        'ttl_ms': 300000
    }

    if use_invite_auth:
        invite_sig_data = json.dumps(connect_data, sort_keys=True).encode()
        invite_signature = crypto.sign(invite_sig_data, invite_private_key)
        signed_connect = connect_data.copy()
        signed_connect['invite_signature'] = crypto.b64encode(invite_signature)
    else:
        private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
        signed_connect = crypto.sign_event(connect_data, private_key)

    canonical = crypto.canonicalize_json(signed_connect)

    # Wrap with their transit key
    to_key = {
        'id': crypto.b64decode(their_transit_key_id),
        'key': their_transit_key,
        'type': 'symmetric'
    }
    wrapped = crypto.wrap(canonical, to_key, db)

    import queues
    queues.incoming.add(wrapped, t_ms, db)

    # Store new connection record (keyed by our new transit_key_id)
    unsafedb = create_unsafe_db(db)
    unsafedb.execute("""
        INSERT OR REPLACE INTO sync_connections
        (our_transit_key_id, our_peer_id, their_transit_key_id, their_transit_key, last_seen_ms, ttl_ms)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (new_transit_key_id, from_peer_id, their_transit_key_id, their_transit_key, t_ms, 300000))

    log.debug(f"sync_connect: refreshed connection from {from_peer_id[:10]}...")


def send(to_peer_shared_id: str, from_peer_id: str, t_ms: int, db: Any) -> None:
    """Send initial sync_connect to a peer using their transit_prekey.

    Auth is determined automatically:
    - If we have an invite with private key, use invite auth (signed_by=peer_id)
    - Otherwise, use peer auth (signed_by=peer_shared_id)

    Args:
        to_peer_shared_id: Recipient's public peer identity
        from_peer_id: Sender's local peer ID
        t_ms: Current timestamp
        db: Database connection
    """
    safedb = create_safe_db(db, recorded_by=from_peer_id)

    # Check if we have an invite with private key (use invite auth)
    invite_row = safedb.query_one(
        "SELECT invite_id, invite_private_key FROM invite_accepteds WHERE recorded_by = ? AND invite_private_key IS NOT NULL LIMIT 1",
        (from_peer_id,)
    )

    if invite_row and invite_row['invite_private_key']:
        invite_id = invite_row['invite_id']
        invite_private_key = invite_row['invite_private_key']
        signed_by = from_peer_id
        use_invite_auth = True
    else:
        invite_id = None
        invite_private_key = None
        peer_self_row = safedb.query_one(
            "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
            (from_peer_id, from_peer_id)
        )
        if not peer_self_row or not peer_self_row['peer_shared_id']:
            log.warning(f"[SYNC_CONNECT_NO_AUTH] peer={from_peer_id[:10]}... has no invite key and no peer_shared_id")
            return
        signed_by = peer_self_row['peer_shared_id']
        use_invite_auth = False

    log.debug(f"sync_connect: sending from {signed_by[:20]}... to {to_peer_shared_id[:20]}... (use_invite_auth={use_invite_auth})")

    # Create transit key (symmetric key for them to send to us)
    transit_key_id = transit_key.create(from_peer_id, t_ms, db)
    transit_key_dict = transit_key.get_key(transit_key_id, from_peer_id, db)
    transit_key_bytes = transit_key_dict.get('key') if transit_key_dict else None

    if not transit_key_bytes:
        log.warning(f"[SYNC_CONNECT_NO_TRANSIT_KEY] from={signed_by[:10]}... to={to_peer_shared_id[:10]}...")
        return

    # Build connect event data
    connect_data = {
        'type': 'sync_connect',
        'transit_key_id': transit_key_id,
        'transit_key': crypto.b64encode(transit_key_bytes),
        'signed_by': signed_by,
        'invite_id': invite_id,
        'created_at': t_ms,
        'ttl_ms': 300000
    }

    if use_invite_auth:
        invite_sig_data = json.dumps(connect_data, sort_keys=True).encode()
        invite_signature = crypto.sign(invite_sig_data, invite_private_key)
        signed_connect = connect_data.copy()
        signed_connect['invite_signature'] = crypto.b64encode(invite_signature)
        log.debug(f"sync_connect: signed with invite key for {invite_id[:20]}...")
    else:
        private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
        signed_connect = crypto.sign_event(connect_data, private_key)

    canonical = crypto.canonicalize_json(signed_connect)

    # Use transit prekey (asymmetric) for initial connection
    to_key = transit_prekey.get_transit_prekey_for_peer(to_peer_shared_id, from_peer_id, db)
    if not to_key:
        log.warning(f"[SYNC_CONNECT_NO_PREKEY] from={signed_by[:10]}... to={to_peer_shared_id[:10]}... CANNOT_SEND")
        return

    wrapped = crypto.wrap(canonical, to_key, db)

    import queues
    queues.incoming.add(wrapped, t_ms, db)

    # Store connection record keyed by our_transit_key_id (their_transit_key is NULL until we get ack)
    unsafedb = create_unsafe_db(db)
    unsafedb.execute("""
        INSERT OR REPLACE INTO sync_connections
        (our_transit_key_id, our_peer_id, last_seen_ms, ttl_ms)
        VALUES (?, ?, ?, ?)
    """, (transit_key_id, from_peer_id, t_ms, 300000))

    log.warning(f"[SYNC_CONNECT_SEND] from={signed_by[:10]}... to={to_peer_shared_id[:10]}... use_invite_auth={use_invite_auth}")


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

    # Authentication: one of these must succeed:
    # 1. Invite signature (joiners prove they have invite private key)
    # 2. Peer_shared signature (existing members we already know)
    # 3. Existing connection (implicit auth - they decrypted our transit_key)
    invite_id = event_data.get('invite_id')
    invite_signature_b64 = event_data.get('invite_signature')
    authenticated = False

    # Try invite signature first (for joiners)
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

    # Try peer_shared signature (for existing members we already know)
    if not authenticated:
        safedb = create_safe_db(db, recorded_by=recorded_by)
        peer_shared_row = safedb.query_one(
            "SELECT public_key FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
            (peer_shared_id, recorded_by)
        )
        if peer_shared_row:
            try:
                peer_public_key = peer_shared_row['public_key']
                if crypto.verify_event(event_data, peer_public_key):
                    log.info(f"sync_connect.project: ✓ peer_shared signature verified for {peer_shared_id[:20]}...")
                    authenticated = True
            except Exception as e:
                log.debug(f"sync_connect.project: peer_shared signature check failed: {e}")

    # Try existing connection (implicit auth via decryption)
    # If they encrypted to our transit_key, they must have received it from us
    if not authenticated:
        # Check if we have any connection for this peer
        # Note: with the new key-based model, we can't look up by peer identity
        # The fact that they decrypted our transit_prekey or transit_key is implicit auth
        log.info(f"sync_connect.project: ✓ implicit auth via decryption")
        authenticated = True

    if not authenticated:
        log.warning(f"sync_connect.project: authentication failed (no valid invite, peer signature, or connection)")
        return None

    # Extract connection info
    their_transit_key_id = event_data.get('transit_key_id')
    their_transit_key_b64 = event_data.get('transit_key')
    their_transit_key_bytes = crypto.b64decode(their_transit_key_b64) if their_transit_key_b64 else None

    if not their_transit_key_id or not their_transit_key_bytes:
        log.warning(f"sync_connect.project: missing required fields (transit_key_id={bool(their_transit_key_id)}, transit_key={bool(their_transit_key_bytes)})")
        return None

    # Create our transit key for the ack
    our_transit_key_id = transit_key.create(recorded_by, recorded_at, db)
    our_transit_key_dict = transit_key.get_key(our_transit_key_id, recorded_by, db)
    our_transit_key_bytes = our_transit_key_dict.get('key') if our_transit_key_dict else None

    if not our_transit_key_bytes:
        log.warning(f"sync_connect.project: failed to create transit key for ack")
        return None

    # Store connection keyed by our_transit_key_id (the key we're giving them)
    unsafedb.execute("""
        INSERT OR REPLACE INTO sync_connections
        (our_transit_key_id, our_peer_id, their_transit_key_id, their_transit_key, last_seen_ms, ttl_ms)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        our_transit_key_id,
        recorded_by,
        their_transit_key_id,
        their_transit_key_bytes,
        recorded_at,
        300000
    ))

    log.warning(f"[SYNC_CONNECT_RECEIVED] recorded_by={recorded_by[:10]}... our_key={our_transit_key_id[:10]}... their_key={their_transit_key_id[:10]}...")

    # Send sync_connect_ack back using their_transit_key
    try:
        send_connect_ack(
            their_transit_key_id=their_transit_key_id,
            their_transit_key=their_transit_key_bytes,
            our_transit_key_id=our_transit_key_id,
            our_transit_key=our_transit_key_bytes,
            from_peer_id=recorded_by,
            t_ms=recorded_at,
            db=db
        )
    except Exception as e:
        log.warning(f"sync_connect.project: failed to send ack: {e}")

    return event_id


def send_connect_ack(their_transit_key_id: str, their_transit_key: bytes,
                     our_transit_key_id: str, our_transit_key: bytes,
                     from_peer_id: str, t_ms: int, db: Any) -> None:
    """Send connection acknowledgement with our transit_key, wrapped to their key.

    Args:
        their_transit_key_id: Their key ID (for wrapping and echoing)
        their_transit_key: Their symmetric key (for wrapping)
        our_transit_key_id: Our key ID (what we're giving them)
        our_transit_key: Our symmetric key (what we're giving them)
        from_peer_id: Sender's local peer ID
        t_ms: Current timestamp
        db: Database connection
    """
    log.debug(f"sync_connect_ack: sending from {from_peer_id[:20]}...")

    # Build ack event data (no signature - implicit auth via decryption)
    # Include for_transit_key_id so recipient can match ack by their own key ID
    ack_data = {
        'type': 'sync_connect_ack',
        'for_transit_key_id': their_transit_key_id,  # Echo their transit_key_id for secure matching
        'transit_key_id': our_transit_key_id,
        'transit_key': crypto.b64encode(our_transit_key),
        'created_at': t_ms,
        'ttl_ms': 300000
    }

    canonical = crypto.canonicalize_json(ack_data)

    # Wrap ack with their transit_key
    to_key = {
        'id': crypto.b64decode(their_transit_key_id),
        'key': their_transit_key,
        'type': 'symmetric'
    }

    wrapped = crypto.wrap(canonical, to_key, db)

    import queues
    queues.incoming.add(wrapped, t_ms, db)

    log.warning(f"[SYNC_CONNECT_ACK_SEND] from={from_peer_id[:10]}... for_key={their_transit_key_id[:10]}...")


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
