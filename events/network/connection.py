"""Connection event type (LOCAL-ONLY) for establishing peer connections.

Unified event type replacing sync_connect, sync_connect_ack, and transit_key.
connection_id is the universal identifier for routing, display, and dependencies.

Event modes:
- mode=req: Connection request with fresh symmetric key
- mode=ack: Connection acknowledgement referencing the request

Flow:
1. Alice creates connection(mode=req) with fresh symmetric key
2. Alice wraps request with Bob's connection_prekey (asymmetric)
3. Bob receives, unwraps, projects → stores Alice's key
4. Bob creates connection(mode=ack) referencing Alice's request
5. Bob wraps ack with Alice's symmetric key
6. Alice receives, unwraps, projects → stores Bob's key
7. Connection ACTIVE (both have symmetric keys)

Connection states:
- PENDING: We sent request, awaiting ack (their_connection_id IS NULL)
- ACTIVE: Bidirectional (both connection_id and their_connection_id set)
- BOOTSTRAP: Connection via invite (invite_id set, peer_shared_id may be NULL)
"""
from dataclasses import dataclass
from typing import Any

# Registry metadata
EVENT_TYPE = 'connection'
SHAREABLE = False  # Local-only - contains symmetric key material
EPHEMERAL = True   # Drop if deps missing - sender will retry
PROJECTION_TABLE = 'connections'

import logging
import json
from core import crypto
from core import store
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)

# Connection TTL (5 minutes default)
CONNECTION_TTL_MS = 300_000


def get_address_for_peer(peer_shared_id: str, recorded_by: str, db: Any) -> tuple[str, int] | None:
    """Look up last known address for a peer from connections table.

    Args:
        peer_shared_id: Peer's shared ID to look up
        recorded_by: Local peer's ID (for scoped lookup)
        db: Database connection

    Returns:
        (ip, port) tuple or None if no address known
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one("""
        SELECT from_addr_ip, from_addr_port FROM connections
        WHERE peer_shared_id = ? AND recorded_by = ?
          AND from_addr_ip IS NOT NULL
        ORDER BY last_handshake_ms DESC LIMIT 1
    """, (peer_shared_id, recorded_by))
    if row and row['from_addr_ip']:
        return (row['from_addr_ip'], row['from_addr_port'])
    return None


def create_request(
    to_peer_shared_id: str | None,
    from_peer_id: str,
    from_peer_shared_id: str | None,
    invite_id: str | None,
    t_ms: int,
    db: Any
) -> tuple[str, bytes]:
    """Create a connection request with fresh symmetric key.

    Args:
        to_peer_shared_id: Remote peer's public identity (NULL for bootstrap)
        from_peer_id: Local peer ID creating the request
        from_peer_shared_id: Local peer's public identity (NULL in bootstrap mode before cascade)
        invite_id: Invite ID for bootstrap connections
        t_ms: Timestamp
        db: Database connection

    Returns:
        (connection_id, symmetric_key): The event ID and key bytes
    """
    if not to_peer_shared_id and not invite_id:
        raise ValueError("At least one of to_peer_shared_id or invite_id must be provided")

    # In bootstrap mode (no peer_shared_id yet), use invite_id as signed_by
    signed_by = from_peer_shared_id if from_peer_shared_id else invite_id

    log.debug(f"connection.create_request: from={signed_by[:20] if signed_by else 'unknown'}... to={to_peer_shared_id[:20] if to_peer_shared_id else invite_id[:20]}...")

    # Generate fresh symmetric key
    symmetric_key = crypto.generate_secret()

    # Build connection request event
    event_data = {
        'type': 'connection',
        'mode': 'req',
        'key': crypto.b64encode(symmetric_key),
        'to_peer_shared_id': to_peer_shared_id,
        'invite_id': invite_id,
        'signed_by': signed_by,
        'created_at': t_ms,
        'ttl_ms': CONNECTION_TTL_MS
    }

    # Sign with peer's private key
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # If invite_id present, also sign with invite private key for bootstrap auth
    # Check both group_prekeys (inviter) and invite_accepteds (joiner) for the key
    if invite_id:
        safedb = create_safe_db(db, recorded_by=from_peer_id)
        invite_private_key = None

        # First try group_prekeys (inviter's case - we created the invite)
        invite_key_row = safedb.query_one(
            "SELECT private_key FROM group_prekeys WHERE owner_peer_id = ? AND recorded_by = ? LIMIT 1",
            (from_peer_id, from_peer_id)
        )
        if invite_key_row and invite_key_row['private_key']:
            invite_private_key = invite_key_row['private_key']

        # Then try invite_accepteds (joiner's case - we accepted an invite)
        if not invite_private_key:
            ia_row = safedb.query_one(
                "SELECT invite_private_key FROM invite_accepteds WHERE invite_id = ? AND recorded_by = ?",
                (invite_id, from_peer_id)
            )
            if ia_row and ia_row['invite_private_key']:
                invite_private_key = ia_row['invite_private_key']

        if invite_private_key:
            invite_sig_data = json.dumps(signed_event, sort_keys=True).encode()
            invite_signature = crypto.sign(invite_sig_data, invite_private_key)
            signed_event['invite_signature'] = crypto.b64encode(invite_signature)
            log.debug(f"connection.create_request: added invite_signature for invite {invite_id[:20]}...")

    # Store the event
    blob = crypto.canonicalize_json(signed_event)
    unsafedb = create_unsafe_db(db)
    connection_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)

    log.info(f"connection.create_request: created {connection_id[:20]}... for {to_peer_shared_id[:20] if to_peer_shared_id else invite_id[:20]}...")

    return connection_id, symmetric_key


def create_ack(
    for_connection_id: str,
    from_peer_id: str,
    from_peer_shared_id: str,
    t_ms: int,
    db: Any
) -> tuple[str, bytes]:
    """Create a connection acknowledgement referencing a request.

    Args:
        for_connection_id: The request's connection_id being acknowledged
        from_peer_id: Local peer ID creating the ack
        from_peer_shared_id: Local peer's public identity
        t_ms: Timestamp
        db: Database connection

    Returns:
        (connection_id, symmetric_key): The ack's event ID and key bytes
    """
    log.debug(f"connection.create_ack: from={from_peer_shared_id[:20]}... for={for_connection_id[:20]}...")

    # Generate fresh symmetric key for the ack
    symmetric_key = crypto.generate_secret()

    # Build connection ack event
    event_data = {
        'type': 'connection',
        'mode': 'ack',
        'for_connection_id': for_connection_id,
        'key': crypto.b64encode(symmetric_key),
        'signed_by': from_peer_shared_id,
        'created_at': t_ms,
        'ttl_ms': CONNECTION_TTL_MS
    }

    # Sign with peer's private key (implicit auth via decryption)
    # Note: Ack is wrapped with their symmetric key, so signature is optional
    # but included for consistency
    private_key = peer.get_private_key(from_peer_id, from_peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store the event
    blob = crypto.canonicalize_json(signed_event)
    unsafedb = create_unsafe_db(db)
    connection_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)

    log.info(f"connection.create_ack: created {connection_id[:20]}... for request {for_connection_id[:20]}...")

    return connection_id, symmetric_key


def project(event_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project connection event into connections table.

    For mode=req: Creates new connection entry with our_key
    For mode=ack: Updates existing connection with their_connection_id and their_key

    Args:
        event_id: The connection event ID
        recorded_by: Local peer who received this event
        recorded_at: When received
        db: Database connection

    Returns:
        event_id on success, None on failure
    """
    log.debug(f"connection.project: event_id={event_id[:20]}... recorded_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(event_id, unsafedb)
    if not blob:
        log.warning(f"connection.project: blob not found for {event_id[:20]}...")
        return None

    event_data = crypto.parse_json(blob)
    mode = event_data.get('mode')

    if mode == 'req':
        return _project_request(event_id, event_data, recorded_by, recorded_at, safedb, unsafedb, db)
    elif mode == 'ack':
        return _project_ack(event_id, event_data, recorded_by, recorded_at, safedb, unsafedb, db)
    else:
        log.warning(f"connection.project: unknown mode '{mode}' for {event_id[:20]}...")
        return None


def _project_request(
    event_id: str,
    event_data: dict,
    recorded_by: str,
    recorded_at: int,
    safedb: Any,
    unsafedb: Any,
    db: Any
) -> str | None:
    """Project a connection request (mode=req)."""
    peer_shared_id = event_data.get('signed_by')
    to_peer_shared_id = event_data.get('to_peer_shared_id')
    invite_id = event_data.get('invite_id')
    key_b64 = event_data.get('key')
    created_at = event_data.get('created_at', recorded_at)
    ttl_ms = event_data.get('ttl_ms', CONNECTION_TTL_MS)

    # ENFORCEMENT: Reject connections from removed peers
    if peer_shared_id:
        removed_check = unsafedb.query_one(
            "SELECT 1 FROM removed_peers WHERE peer_shared_id = ? LIMIT 1",
            (peer_shared_id,)
        )
        if removed_check:
            log.info(f"connection.project: rejecting request from removed peer {peer_shared_id[:20]}...")
            return None

    # Authentication: Try invite signature first (for bootstrap), fall back to peer signature
    authenticated = False
    invite_signature_b64 = event_data.get('invite_signature')

    if invite_id and invite_signature_b64:
        invite_public_key = None

        # Get invite_pubkey from either:
        # - invites table (inviter projected the invite they created)
        # - invite_accepteds table (joiner projected invite_accepted from URL)
        pubkey_row = safedb.query_one(
            """SELECT invite_pubkey FROM invites WHERE invite_id = ? AND recorded_by = ?
               UNION
               SELECT invite_pubkey FROM invite_accepteds WHERE invite_id = ? AND recorded_by = ?
               LIMIT 1""",
            (invite_id, recorded_by, invite_id, recorded_by)
        )
        if pubkey_row and pubkey_row['invite_pubkey']:
            invite_public_key = crypto.b64decode(pubkey_row['invite_pubkey'])
            log.info(f"connection.project: found invite_pubkey for {invite_id[:20]}...")

        if invite_public_key:
            # Verify invite signature
            connect_without_invite_sig = {k: v for k, v in event_data.items() if k != 'invite_signature'}
            sig_data = json.dumps(connect_without_invite_sig, sort_keys=True).encode()
            invite_signature = crypto.b64decode(invite_signature_b64)
            if crypto.verify(sig_data, invite_signature, invite_public_key):
                log.info(f"connection.project: invite signature verified for {invite_id[:20]}...")
                authenticated = True

    if not authenticated:
        if crypto.verify_signed_by_peer_shared(event_data, recorded_by, db):
            log.debug(f"connection.project: peer signature verified")
            authenticated = True

    if not authenticated:
        log.warning(f"connection.project: authentication failed for request {event_id[:20]}...")
        return None

    # Extract key
    if not key_b64:
        log.warning(f"connection.project: missing key in request {event_id[:20]}...")
        return None

    symmetric_key = crypto.b64decode(key_b64)

    # Look up from_addr from packet_metadata staging table
    from_addr_row = safedb.query_one("""
        SELECT from_addr_ip, from_addr_port FROM packet_metadata
        WHERE event_id = ? AND recorded_by = ?
    """, (event_id, recorded_by))
    from_addr_ip = from_addr_row['from_addr_ip'] if from_addr_row else None
    from_addr_port = from_addr_row['from_addr_port'] if from_addr_row else None

    if from_addr_ip:
        log.info(f"connection.project: received request {event_id[:20]}... from {peer_shared_id[:20] if peer_shared_id else 'unknown'}... at {from_addr_ip}:{from_addr_port}")
    else:
        log.info(f"connection.project: received request {event_id[:20]}... from {peer_shared_id[:20] if peer_shared_id else 'unknown'}...")

    # NOTE: We do NOT store a connection row or pending request here.
    # The responder's connection row is created in _send_ack_for_request with
    # connection_id = ack_id (not request_id). This ensures routing works correctly:
    # when the ack is sent with hint = request_id[:16], only the original requester's
    # connection row (where connection_id = request_id) matches for decryption.

    # Update address for existing connection if we have one for this peer
    if peer_shared_id and from_addr_ip:
        safedb.execute("""
            UPDATE connections
            SET from_addr_ip = ?, from_addr_port = ?
            WHERE peer_shared_id = ? AND recorded_by = ?
        """, (from_addr_ip, from_addr_port, peer_shared_id, recorded_by))

    # Send ack back (this creates our connection row with connection_id = ack_id)
    _send_ack_for_request(event_id, peer_shared_id, symmetric_key, recorded_by, recorded_at, db)

    return event_id


def _project_ack(
    event_id: str,
    event_data: dict,
    recorded_by: str,
    recorded_at: int,
    safedb: Any,
    unsafedb: Any,
    db: Any
) -> str | None:
    """Project a connection acknowledgement (mode=ack)."""
    for_connection_id = event_data.get('for_connection_id')
    peer_shared_id = event_data.get('signed_by')
    key_b64 = event_data.get('key')

    if not for_connection_id:
        log.warning(f"connection.project: ack {event_id[:20]}... missing for_connection_id")
        return None

    if not key_b64:
        log.warning(f"connection.project: ack {event_id[:20]}... missing key")
        return None

    their_key = crypto.b64decode(key_b64)

    # Find the original request we sent
    existing = safedb.query_one("""
        SELECT connection_id FROM connections
        WHERE connection_id = ? AND recorded_by = ?
    """, (for_connection_id, recorded_by))

    if not existing:
        log.warning(f"connection.project: no matching request for ack {event_id[:20]}... (for_connection_id={for_connection_id[:20]}...)")
        return None

    # Look up from_addr from packet_metadata staging table
    from_addr_row = safedb.query_one("""
        SELECT from_addr_ip, from_addr_port FROM packet_metadata
        WHERE event_id = ? AND recorded_by = ?
    """, (event_id, recorded_by))
    from_addr_ip = from_addr_row['from_addr_ip'] if from_addr_row else None
    from_addr_port = from_addr_row['from_addr_port'] if from_addr_row else None

    # Update connection with their info + address
    safedb.execute("""
        UPDATE connections
        SET their_connection_id = ?,
            their_key = ?,
            peer_shared_id = COALESCE(peer_shared_id, ?),
            last_handshake_ms = ?,
            from_addr_ip = COALESCE(?, from_addr_ip),
            from_addr_port = COALESCE(?, from_addr_port)
        WHERE connection_id = ? AND recorded_by = ?
    """, (
        event_id, their_key, peer_shared_id,
        recorded_at, from_addr_ip, from_addr_port,
        for_connection_id, recorded_by
    ))

    if from_addr_ip:
        log.info(f"connection.project: activated connection {for_connection_id[:20]}... with ack {event_id[:20]}... at {from_addr_ip}:{from_addr_port}")
    else:
        log.info(f"connection.project: activated connection {for_connection_id[:20]}... with ack {event_id[:20]}...")

    return event_id


def _send_ack_for_request(
    request_id: str,
    remote_peer_shared_id: str | None,
    their_key: bytes,
    local_peer_id: str,
    t_ms: int,
    db: Any
) -> None:
    """Send connection ack in response to a request."""
    from core.db import create_safe_db

    safedb = create_safe_db(db, recorded_by=local_peer_id)

    # Get our peer_shared_id
    peer_self_row = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (local_peer_id, local_peer_id)
    )
    if not peer_self_row:
        log.warning(f"connection._send_ack: no peer_shared_id for {local_peer_id[:20]}...")
        return

    local_peer_shared_id = peer_self_row['peer_shared_id']

    # Check if we already have a connection to this peer (from our own request)
    # If so, update it instead of creating a new one
    existing = safedb.query_one("""
        SELECT connection_id, our_key FROM connections
        WHERE peer_shared_id = ? AND recorded_by = ?
        ORDER BY created_at DESC LIMIT 1
    """, (remote_peer_shared_id, local_peer_id))

    if existing:
        # Update existing connection with their key
        safedb.execute("""
            UPDATE connections
            SET their_key = ?,
                their_connection_id = ?,
                last_handshake_ms = ?
            WHERE connection_id = ? AND recorded_by = ?
        """, (their_key, request_id, t_ms, existing['connection_id'], local_peer_id))

        # Create and send ack using our existing key
        ack_id, _ = create_ack(
            for_connection_id=request_id,
            from_peer_id=local_peer_id,
            from_peer_shared_id=local_peer_shared_id,
            t_ms=t_ms,
            db=db
        )

        # Wrap ack with their symmetric key and send via transport
        unsafedb = create_unsafe_db(db)
        ack_blob = store.get(ack_id, unsafedb)
        to_key = {
            'id': crypto.b64decode(request_id)[:16],
            'key': their_key,
            'type': 'symmetric'
        }
        wrapped = crypto.wrap(ack_blob, to_key, db)

        from core import transport
        # Try to look up destination address
        to_addr = get_address_for_peer(remote_peer_shared_id, local_peer_id, db)
        if not to_addr:
            to_addr = transport.get_peer_address(remote_peer_shared_id)

        if to_addr:
            from_addr = transport.get_listen_address() or ('127.0.0.1', 0)
            transport.send(wrapped, from_addr, to_addr)
        else:
            transport.deliver(wrapped, ('127.0.0.1', 0))

        log.info(f"connection._send_ack: updated existing connection and sent ack {ack_id[:20]}... for request {request_id[:20]}...")
        return

    # No existing connection - create new one
    ack_id, ack_key = create_ack(
        for_connection_id=request_id,
        from_peer_id=local_peer_id,
        from_peer_shared_id=local_peer_shared_id,
        t_ms=t_ms,
        db=db
    )

    # Store our outgoing connection (we're the one who received the request)
    # Our connection_id is the ack_id, their_connection_id is the request_id
    safedb.execute("""
        INSERT OR REPLACE INTO connections (
            connection_id, recorded_by, peer_shared_id, invite_id,
            our_key, their_connection_id, their_key,
            created_at, last_handshake_ms, ttl_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ack_id, local_peer_id, remote_peer_shared_id, None,
        ack_key, request_id, their_key,
        t_ms, t_ms, CONNECTION_TTL_MS
    ))

    # Wrap ack with their symmetric key and send via transport
    unsafedb = create_unsafe_db(db)
    ack_blob = store.get(ack_id, unsafedb)

    to_key = {
        'id': crypto.b64decode(request_id)[:16],  # Use request_id as hint
        'key': their_key,
        'type': 'symmetric'
    }

    wrapped = crypto.wrap(ack_blob, to_key, db)

    from core import transport
    # Try to look up destination address
    to_addr = get_address_for_peer(remote_peer_shared_id, local_peer_id, db)
    if not to_addr:
        to_addr = transport.get_peer_address(remote_peer_shared_id)

    if to_addr:
        from_addr = transport.get_listen_address() or ('127.0.0.1', 0)
        transport.send(wrapped, from_addr, to_addr)
    else:
        transport.deliver(wrapped, ('127.0.0.1', 0))

    log.info(f"connection._send_ack: sent ack {ack_id[:20]}... for request {request_id[:20]}...")


# ============================================================================
# Connection management functions
# ============================================================================

def send_to_all(t_ms: int, db: Any) -> None:
    """Send connection requests from all local peers to all known peers.

    Called by tick() to establish/refresh connections.
    Skips peers we already have active connections with.

    Args:
        t_ms: Current timestamp in milliseconds
        db: Database connection
    """
    unsafedb = create_unsafe_db(db)
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")

    log.info(f"connection.send_to_all: found {len(local_peer_rows)} local peers at t_ms={t_ms}")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']
        safedb = create_safe_db(db, recorded_by=peer_id)

        # Get our peer_shared_id (may be None during bootstrap before cascade completes)
        peer_self_row = safedb.query_one(
            "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
            (peer_id, peer_id)
        )
        peer_shared_id = peer_self_row['peer_shared_id'] if peer_self_row else None

        # Get invite data if joiner - includes inviter's peer_shared_id and transit prekey
        # Check this FIRST because bootstrap mode needs it even without peer_self
        invite_row = safedb.query_one(
            """SELECT invite_id, inviter_peer_shared_id,
                      inviter_transit_prekey_id, inviter_transit_prekey_public_key
               FROM invite_accepteds WHERE recorded_by = ? LIMIT 1""",
            (peer_id,)
        )
        invite_id = invite_row['invite_id'] if invite_row else None

        # BOOTSTRAP MODE: No peer_self yet, but we have invite_accepteds
        # Connect to inviter using invite authentication
        if not peer_shared_id:
            if invite_row and invite_row['inviter_peer_shared_id']:
                inviter_id = invite_row['inviter_peer_shared_id']
                log.info(f"connection.send_to_all: BOOTSTRAP MODE for {peer_id[:20]}... -> inviter {inviter_id[:20]}...")

                # Check if we already have a connection to inviter
                existing = safedb.query_one("""
                    SELECT 1 FROM connections
                    WHERE peer_shared_id = ? AND recorded_by = ?
                      AND last_handshake_ms + ttl_ms > ?
                """, (inviter_id, peer_id, t_ms))

                if not existing:
                    # Build transit prekey dict from invite_accepteds
                    inviter_transit_prekey = None
                    if invite_row.get('inviter_transit_prekey_public_key'):
                        inviter_transit_prekey = {
                            'id': crypto.b64decode(invite_row['inviter_transit_prekey_id'])[:16],
                            'public_key': invite_row['inviter_transit_prekey_public_key'],
                            'type': 'asymmetric'
                        }

                    try:
                        _send_request(
                            to_peer_shared_id=inviter_id,
                            from_peer_id=peer_id,
                            from_peer_shared_id=None,  # Bootstrap mode - no peer_shared_id yet
                            invite_id=invite_id,
                            t_ms=t_ms,
                            db=db,
                            inviter_transit_prekey=inviter_transit_prekey,
                        )
                    except Exception as e:
                        log.warning(f"connection.send_to_all: bootstrap error: {e}")
            continue  # Skip normal mode for this peer

        # NORMAL MODE: Have peer_self, can connect to all known peers

        # Two types of connections:
        # 1. peers_shared: actual synced peers (peer_shared auth, permanent)
        # 2. transit_prekeys_shared: includes predicted IDs from invites (invite auth, expires)
        all_peer_ids = set()

        for row in safedb.query("SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?", (peer_id,)):
            all_peer_ids.add(row['peer_shared_id'])

        # Also include peers we have transit_prekeys_shared for (may include predicted invite IDs)
        for row in safedb.query(
            "SELECT peer_id FROM transit_prekeys_shared WHERE recorded_by = ?",
            (peer_id,)
        ):
            all_peer_ids.add(row['peer_id'])

        # Add inviter's peer_shared_id from invite_accepteds
        if invite_row and invite_row['inviter_peer_shared_id']:
            all_peer_ids.add(invite_row['inviter_peer_shared_id'])
            log.debug(f"connection.send_to_all: added inviter {invite_row['inviter_peer_shared_id'][:20]}... from invite_accepteds")

        # Send to each peer
        for to_peer_shared_id in all_peer_ids:
            if to_peer_shared_id == peer_shared_id:
                continue  # Skip self

            # Skip if already have ANY unexpired connection (active or pending)
            # We only need one pending request per peer - no point sending duplicates
            existing = safedb.query_one("""
                SELECT 1 FROM connections
                WHERE peer_shared_id = ? AND recorded_by = ?
                  AND last_handshake_ms + ttl_ms > ?
            """, (to_peer_shared_id, peer_id, t_ms))

            if existing:
                log.debug(f"connection.send_to_all: skipping {to_peer_shared_id[:20]}... (connection exists)")
                continue

            try:
                _send_request(
                    to_peer_shared_id=to_peer_shared_id,
                    from_peer_id=peer_id,
                    from_peer_shared_id=peer_shared_id,
                    invite_id=invite_id,
                    t_ms=t_ms,
                    db=db
                )
            except Exception as e:
                log.warning(f"connection.send_to_all: error sending to {to_peer_shared_id[:20]}...: {e}")


def _send_request(
    to_peer_shared_id: str,
    from_peer_id: str,
    from_peer_shared_id: str | None,
    invite_id: str | None,
    t_ms: int,
    db: Any,
    inviter_transit_prekey: dict | None = None,
) -> None:
    """Send a connection request to a specific peer.

    Args:
        to_peer_shared_id: Remote peer's public identity
        from_peer_id: Local peer ID
        from_peer_shared_id: Local peer's public identity (None in bootstrap mode)
        invite_id: Invite ID for bootstrap auth
        t_ms: Timestamp
        db: Database connection
        inviter_transit_prekey: Pre-fetched transit prekey dict (for bootstrap mode)
    """
    from events.network import connection_prekey

    # Create request
    connection_id, symmetric_key = create_request(
        to_peer_shared_id=to_peer_shared_id,
        from_peer_id=from_peer_id,
        from_peer_shared_id=from_peer_shared_id,
        invite_id=invite_id,
        t_ms=t_ms,
        db=db
    )

    # Store our outgoing connection (PENDING)
    safedb = create_safe_db(db, recorded_by=from_peer_id)
    safedb.execute("""
        INSERT OR REPLACE INTO connections (
            connection_id, recorded_by, peer_shared_id, invite_id,
            our_key, their_connection_id, their_key,
            created_at, last_handshake_ms, ttl_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        connection_id, from_peer_id, to_peer_shared_id, invite_id,
        symmetric_key, None, None,  # PENDING - no their_* yet
        t_ms, t_ms, CONNECTION_TTL_MS
    ))

    # Get recipient's connection_prekey - try multiple sources
    to_key = None

    # 1. Pre-passed transit prekey (bootstrap mode with prekey from invite_accepteds)
    if inviter_transit_prekey:
        to_key = inviter_transit_prekey
        log.debug(f"connection._send_request: using pre-passed transit prekey")

    # 2. Look up from transit_prekeys_shared table (normal mode)
    if not to_key:
        to_key = connection_prekey.get_prekey_for_peer(to_peer_shared_id, from_peer_id, db)

    # 3. BOOTSTRAP FALLBACK: Check invite_accepteds for inviter's transit prekey
    if not to_key:
        inviter_row = safedb.query_one("""
            SELECT inviter_transit_prekey_id, inviter_transit_prekey_public_key
            FROM invite_accepteds
            WHERE inviter_peer_shared_id = ? AND recorded_by = ?
            LIMIT 1
        """, (to_peer_shared_id, from_peer_id))

        if inviter_row and inviter_row['inviter_transit_prekey_public_key']:
            to_key = {
                'id': crypto.b64decode(inviter_row['inviter_transit_prekey_id'])[:16],
                'public_key': inviter_row['inviter_transit_prekey_public_key'],
                'type': 'asymmetric'
            }
            log.info(f"connection._send_request: using inviter transit prekey from invite_accepteds for {to_peer_shared_id[:20]}...")

    if not to_key:
        log.warning(f"connection._send_request: no prekey for {to_peer_shared_id[:20]}...")
        return

    # Wrap and send via transport
    unsafedb = create_unsafe_db(db)
    request_blob = store.get(connection_id, unsafedb)
    wrapped = crypto.wrap(request_blob, to_key, db)

    from core import transport

    # Try to look up destination address
    to_addr = get_address_for_peer(to_peer_shared_id, from_peer_id, db)
    if not to_addr:
        to_addr = transport.get_peer_address(to_peer_shared_id)

    if to_addr:
        # Real networking - send via transport with addresses
        from_addr = transport.get_listen_address() or ('127.0.0.1', 0)
        transport.send(wrapped, from_addr, to_addr)
        log.info(f"connection._send_request: sent {connection_id[:20]}... to {to_peer_shared_id[:20]}... at {to_addr}")
    else:
        # Loopback mode - deliver directly to incoming queue
        from_addr = ('127.0.0.1', 0)
        transport.deliver(wrapped, from_addr)
        log.info(f"connection._send_request: sent {connection_id[:20]}... to {to_peer_shared_id[:20]}... (loopback)")


def purge_expired(t_ms: int, db: Any) -> int:
    """Remove expired connections.

    Args:
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Number of connections purged
    """
    unsafedb = create_unsafe_db(db)
    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    total_count = 0
    for peer_row in local_peers:
        peer_id = peer_row['peer_id']
        safedb = create_safe_db(db, recorded_by=peer_id)

        count_row = safedb.query_one(
            "SELECT COUNT(*) as cnt FROM connections WHERE recorded_by = ? AND last_handshake_ms + ttl_ms < ?",
            (peer_id, t_ms)
        )
        count = count_row['cnt'] if count_row else 0

        if count > 0:
            safedb.execute(
                "DELETE FROM connections WHERE recorded_by = ? AND last_handshake_ms + ttl_ms < ?",
                (peer_id, t_ms)
            )
            total_count += count

    if total_count > 0:
        log.info(f"connection.purge_expired: purged {total_count} expired connections")

    return total_count


# ============================================================================
# Connection dataclass and display helpers
# ============================================================================

@dataclass
class Connection:
    """Bidirectional channel identified by connection_id, labeled by identity."""

    connection_id: str               # Our connection event ID (mode=req)
    recorded_by: str                 # Local peer who owns this connection

    # Identity labels (at least one set)
    peer_shared_id: str | None       # Remote peer's public identity (NULL until synced)
    invite_id: str | None            # Invite used for this connection (for bootstrap)

    # Keys
    our_key: bytes                   # Symmetric key we created (they send to us)
    their_connection_id: str | None  # Their ack's connection_id (NULL until they ack)
    their_key: bytes | None          # Symmetric key they created (we send to them)

    # Lifecycle
    created_at: int
    last_handshake_ms: int
    ttl_ms: int

    # Sync optimization
    last_synced_root_hash: bytes | None = None  # Skip sync if our root unchanged

    @classmethod
    def from_row(cls, row: dict) -> 'Connection':
        """Create Connection from database row."""
        return cls(
            connection_id=row['connection_id'],
            recorded_by=row['recorded_by'],
            peer_shared_id=row.get('peer_shared_id'),
            invite_id=row.get('invite_id'),
            our_key=row['our_key'],
            their_connection_id=row.get('their_connection_id'),
            their_key=row.get('their_key'),
            created_at=row['created_at'],
            last_handshake_ms=row['last_handshake_ms'],
            ttl_ms=row['ttl_ms'],
            last_synced_root_hash=row.get('last_synced_root_hash'),
        )

    @property
    def label(self) -> str:
        """Human-readable identity: peer_shared_id or invite_id."""
        return self.peer_shared_id or self.invite_id or self.connection_id

    @property
    def short_label(self) -> str:
        """Short version of label for display (first 8 chars)."""
        label = self.label
        return label[:8] + '...' if len(label) > 11 else label

    @property
    def short_connection_id(self) -> str:
        """Short connection_id for display (first 11 chars)."""
        return self.connection_id[:11] + '...' if len(self.connection_id) > 14 else self.connection_id

    def is_active(self, t_ms: int) -> bool:
        """Check if connection is still valid (not expired)."""
        return self.last_handshake_ms + self.ttl_ms > t_ms

    def can_send(self) -> bool:
        """Check if we have their key to send to them."""
        return self.their_key is not None

    def is_pending(self) -> bool:
        """Check if connection is pending (awaiting ack)."""
        return self.their_connection_id is None

    def is_bidirectional(self) -> bool:
        """Check if connection is fully established (both directions)."""
        return self.their_connection_id is not None and self.their_key is not None

    def is_bootstrap(self) -> bool:
        """Check if this is a bootstrap connection (via invite)."""
        return self.invite_id is not None

    def time_since_handshake(self, t_ms: int) -> int:
        """Milliseconds since last handshake (req/ack)."""
        return t_ms - self.last_handshake_ms

    def time_since_traffic(self, t_ms: int) -> int | None:
        """Milliseconds since last traffic. STUB: returns None until implemented."""
        # TODO: Implement when last_traffic_ms is populated
        return None

    def time_until_expiry(self, t_ms: int) -> int:
        """Milliseconds until expiry."""
        return (self.last_handshake_ms + self.ttl_ms) - t_ms


def list_all_for_display(peer_id: str, t_ms: int, db: Any) -> dict:
    """Get all connection data organized for CLI display.

    Args:
        peer_id: Local peer ID
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Dict with:
            'active': List of bidirectional connections
            'pending': List of pending (awaiting ack) connections
            'bootstrap': List of bootstrap connections (via invite)
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    rows = safedb.query("""
        SELECT * FROM connections
        WHERE recorded_by = ? AND last_handshake_ms + ttl_ms > ?
        ORDER BY last_handshake_ms DESC
    """, (peer_id, t_ms))

    connections = [Connection.from_row(row) for row in rows]

    result = {
        'active': [],
        'pending': [],
        'bootstrap': [],
    }

    for conn in connections:
        if conn.is_bidirectional():
            result['active'].append(conn)
        elif conn.is_pending():
            result['pending'].append(conn)
        elif conn.is_bootstrap():
            result['bootstrap'].append(conn)

    return result


def get_active_connection(peer_id: str, remote_peer_shared_id: str, t_ms: int, db: Any) -> Connection | None:
    """Get active bidirectional connection to a specific peer."""
    safedb = create_safe_db(db, recorded_by=peer_id)

    row = safedb.query_one("""
        SELECT * FROM connections
        WHERE peer_shared_id = ?
          AND recorded_by = ?
          AND their_connection_id IS NOT NULL
          AND their_key IS NOT NULL
          AND last_handshake_ms + ttl_ms > ?
        ORDER BY last_handshake_ms DESC
    """, (remote_peer_shared_id, peer_id, t_ms))

    return Connection.from_row(row) if row else None


def has_active_connection(peer_id: str, remote_peer_shared_id: str, t_ms: int, db: Any) -> bool:
    """Check if there's an active bidirectional connection to a peer."""
    return get_active_connection(peer_id, remote_peer_shared_id, t_ms, db) is not None


def format_time_ago(ms: int) -> str:
    """Format milliseconds as human-readable time ago string."""
    if ms < 1000:
        return f"{ms}ms ago"
    elif ms < 60000:
        return f"{ms // 1000}s ago"
    elif ms < 3600000:
        return f"{ms // 60000}m ago"
    else:
        return f"{ms // 3600000}h ago"


def format_time_remaining(ms: int) -> str:
    """Format milliseconds as human-readable time remaining string."""
    if ms <= 0:
        return "expired"
    elif ms < 60000:
        return f"in {ms // 1000}s"
    elif ms < 3600000:
        return f"in {ms // 60000}m {(ms % 60000) // 1000}s"
    else:
        return f"in {ms // 3600000}h {(ms % 3600000) // 60000}m"


# ============================================================================
# User removal support
# ============================================================================

def remove_connections_for_user(user_id: str, db: Any) -> int:
    """Remove all connections associated with a user's peers.

    Called when a user is removed from the network. This removes all connections
    where the remote peer belongs to the specified user, across all local peers.

    Args:
        user_id: The user being removed
        db: Database connection

    Returns:
        Total number of connections removed
    """
    unsafedb = create_unsafe_db(db)
    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    total_removed = 0

    for peer_row in local_peers:
        peer_id = peer_row['peer_id']
        safedb = create_safe_db(db, recorded_by=peer_id)

        # Find peer_shared_ids that belong to this user
        user_peer_rows = safedb.query("""
            SELECT peer_shared_id FROM peers_shared
            WHERE user_id = ? AND recorded_by = ?
        """, (user_id, peer_id))

        for user_peer_row in user_peer_rows:
            peer_shared_id = user_peer_row['peer_shared_id']

            # Count before delete
            count_row = safedb.query_one(
                "SELECT COUNT(*) as cnt FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
                (peer_shared_id, peer_id)
            )
            count = count_row['cnt'] if count_row else 0

            if count > 0:
                safedb.execute(
                    "DELETE FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
                    (peer_shared_id, peer_id)
                )
                total_removed += count
                log.info(f"connection.remove_connections_for_user: removed {count} connections to peer {peer_shared_id[:20]}... for user {user_id[:20]}...")

    if total_removed > 0:
        log.info(f"connection.remove_connections_for_user: total {total_removed} connections removed for user {user_id[:20]}...")

    return total_removed


def remove_connections_for_peer(peer_shared_id: str, db: Any) -> int:
    """Remove all connections to a specific peer.

    Called when a peer is removed. This removes all connections to the specified
    peer_shared_id across all local peers.

    Args:
        peer_shared_id: The peer being removed
        db: Database connection

    Returns:
        Total number of connections removed
    """
    unsafedb = create_unsafe_db(db)
    local_peers = unsafedb.query("SELECT peer_id FROM local_peers")

    total_removed = 0

    for peer_row in local_peers:
        peer_id = peer_row['peer_id']
        safedb = create_safe_db(db, recorded_by=peer_id)

        # Count before delete
        count_row = safedb.query_one(
            "SELECT COUNT(*) as cnt FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
            (peer_shared_id, peer_id)
        )
        count = count_row['cnt'] if count_row else 0

        if count > 0:
            safedb.execute(
                "DELETE FROM connections WHERE peer_shared_id = ? AND recorded_by = ?",
                (peer_shared_id, peer_id)
            )
            total_removed += count

    if total_removed > 0:
        log.info(f"connection.remove_connections_for_peer: removed {total_removed} connections to peer {peer_shared_id[:20]}...")

    return total_removed


# ============================================================================
# Connection query functions (used by sync.py and others)
# ============================================================================

def get_connections(peer_id: str, t_ms: int, db: Any) -> list[Connection]:
    """Get all active connections for a peer.

    Args:
        peer_id: Local peer ID
        t_ms: Current timestamp
        db: Database connection

    Returns:
        List of active Connection objects
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    rows = safedb.query("""
        SELECT * FROM connections
        WHERE recorded_by = ? AND last_handshake_ms + ttl_ms > ?
    """, (peer_id, t_ms))

    return [Connection.from_row(row) for row in rows]


def get_connection_by_peer(peer_id: str, remote_peer_shared_id: str, t_ms: int, db: Any) -> Connection | None:
    """Get connection by remote peer identity.

    Prefers connections that can send (have their_key).

    Args:
        peer_id: Local peer ID
        remote_peer_shared_id: Remote peer's public identity
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Connection if found and active, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Prefer connections with their_key (can send)
    row = safedb.query_one("""
        SELECT * FROM connections
        WHERE peer_shared_id = ? AND recorded_by = ? AND last_handshake_ms + ttl_ms > ?
        ORDER BY (their_key IS NOT NULL) DESC, last_handshake_ms DESC
    """, (remote_peer_shared_id, peer_id, t_ms))

    return Connection.from_row(row) if row else None


def get_connection_by_invite(peer_id: str, invite_id: str, t_ms: int, db: Any) -> Connection | None:
    """Get connection by invite ID (for bootstrap).

    Args:
        peer_id: Local peer ID
        invite_id: Invite used for this connection
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Connection if found and active, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    row = safedb.query_one("""
        SELECT * FROM connections
        WHERE invite_id = ? AND recorded_by = ? AND last_handshake_ms + ttl_ms > ?
    """, (invite_id, peer_id, t_ms))

    return Connection.from_row(row) if row else None


def get_connection_by_their_id(peer_id: str, their_connection_id: str, t_ms: int, db: Any) -> Connection | None:
    """Get connection by the remote party's connection_id.

    When receiving a message from a connection, the sender's connection_id
    is in the message envelope. We need to find OUR connection where
    their_connection_id matches the sender's connection_id.

    Args:
        peer_id: Local peer ID
        their_connection_id: Remote party's connection_id (from received message)
        t_ms: Current timestamp
        db: Database connection

    Returns:
        Connection if found and active, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    row = safedb.query_one("""
        SELECT * FROM connections
        WHERE their_connection_id = ? AND recorded_by = ? AND last_handshake_ms + ttl_ms > ?
    """, (their_connection_id, peer_id, t_ms))

    return Connection.from_row(row) if row else None


# ============================================================================
# Sending on connections
# ============================================================================

def send(recorded_by: str, connection_id: str, blob: bytes, t_ms: int, db: Any) -> bool:
    """Send a blob on an established connection.

    THE interface for all outbound traffic on connections. Handles:
    - Looking up connection by connection_id
    - Wrapping blob with their_key (symmetric)
    - Adding hint (their_connection_id[:16])
    - Queuing to incoming

    Args:
        recorded_by: Local peer ID
        connection_id: Our connection_id for this connection
        blob: Raw blob to send (event data)
        t_ms: Current timestamp
        db: Database connection

    Returns:
        True if sent, False if connection not ready (no their_key)
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    conn = safedb.query_one("""
        SELECT their_connection_id, their_key, peer_shared_id
        FROM connections
        WHERE connection_id = ? AND recorded_by = ?
    """, (connection_id, recorded_by))

    if not conn:
        log.warning(f"connection.send: no connection {connection_id[:20]}...")
        return False

    their_connection_id = conn['their_connection_id']
    their_key = conn['their_key']
    to_peer_shared_id = conn['peer_shared_id']

    if not their_key or not their_connection_id:
        log.debug(f"connection.send: connection {connection_id[:20]}... not ready (no their_key)")
        return False

    # Wrap with their key using their_connection_id as hint
    to_key = {
        'id': crypto.b64decode(their_connection_id)[:16],
        'key': their_key,
        'type': 'symmetric'
    }

    wrapped = crypto.wrap(blob, to_key, db)

    from core import transport
    # Try to look up destination address
    to_addr = get_address_for_peer(to_peer_shared_id, recorded_by, db)
    if not to_addr:
        to_addr = transport.get_peer_address(to_peer_shared_id)

    if to_addr:
        from_addr = transport.get_listen_address() or ('127.0.0.1', 0)
        transport.send(wrapped, from_addr, to_addr)
    else:
        transport.deliver(wrapped, ('127.0.0.1', 0))

    log.debug(f"connection.send: sent {len(blob)}B on {connection_id[:20]}...")
    return True
