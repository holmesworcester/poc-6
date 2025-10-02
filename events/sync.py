from typing import Any
from events import first_seen, key, prekey, peer
import queues
import crypto
import store
import json


def unwrap_and_store(blob: bytes, t_ms: int, db: Any) -> str:
    """Unwrap an incoming transit blob and store it, with its corresponding "first_seen" event blob.

    The seen_by_peer_id is determined by the transit_key (the receiving peer).
    Returns empty string if unwrap fails (missing key, decryption error, etc.).
    """
    import logging
    log = logging.getLogger(__name__)

    hint = key.extract_id(blob)
    hint_b64 = crypto.b64encode(hint)

    # For incoming blobs: seen_by_peer_id = peer who owns the transit_key (the receiving peer)
    seen_by_peer_id = key.get_peer_id_for_key(hint_b64, db)

    if not seen_by_peer_id:
        log.info(f"Skipping storage for blob with id {hint_b64}: transit key not found (peer unknown)")
        return ""

    unwrapped_blob, missing_keys = crypto.unwrap(blob, db)
    if unwrapped_blob is None:
        if missing_keys:
            log.info(f"Skipping storage for blob with id {hint_b64}: missing keys {missing_keys}")
        else:
            log.info(f"Skipping storage for blob with id {hint_b64}: unwrap failed")
        return ""

    # Store the unwrapped event blob
    event_id = store.blob(unwrapped_blob, t_ms, True, db)

    # Create first_seen event for it (don't auto-project yet)
    first_seen_id = first_seen.create(event_id, seen_by_peer_id, t_ms, db, True)

    return first_seen_id


def receive(batch_size: int, t_ms: int, db: Any) -> None:
    """Receive and process a batch of incoming transit blobs."""
    import logging
    log = logging.getLogger(__name__)

    transit_blobs = queues.incoming.drain(batch_size, db)
    log.info(f"Drained {len(transit_blobs)} blobs from incoming queue")

    new_first_seen_ids = [unwrap_and_store(blob, t_ms, db) for blob in transit_blobs]
    log.info(f"Unwrapped {len(new_first_seen_ids)} blobs, got first_seen_ids: {new_first_seen_ids}")

    # Filter out empty strings (failed unwraps)
    valid_first_seen_ids = [id for id in new_first_seen_ids if id]
    log.info(f"Valid first_seen_ids to project: {valid_first_seen_ids}")

    first_seen.project_ids(valid_first_seen_ids, db)

    # Event-driven unblocking now happens automatically in first_seen.project()
    # via queues.blocked.notify_event_valid() - no need for scan-all loop

    db.commit()


def send_requests(from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send sync requests to all peers."""
    peer_rows = db.query("SELECT peer_id FROM peers WHERE peer_id != ?", (from_peer_id,))
    for row in peer_rows:
        send_request(row['peer_id'], from_peer_id, from_peer_shared_id, t_ms, db)
    db.commit()

def send_request(to_peer_id: str, from_peer_id: str, from_peer_shared_id: str, t_ms: int, db: Any) -> None:
    """Send a sync request to a peer.

    Args:
        to_peer_id: Recipient's peer_id
        from_peer_id: Sender's peer_id (for creating transit key and signing)
        from_peer_shared_id: Sender's peer_shared_id (included in request so recipient knows which events to send)
        t_ms: Timestamp
        db: Database connection
    """
    # Create a transit key for the response (owned by requester so they can decrypt response)
    response_transit_key_id = key.create(from_peer_id, t_ms, db)
    response_transit_key = key.get_key(response_transit_key_id, db)

    # Encode transit key for JSON serialization (recipient needs the actual key to wrap responses)
    request_data = {
        'type': 'sync',
        'peer_id': from_peer_id,
        'peer_shared_id': from_peer_shared_id,  # Include so recipient knows which events to send
        'address': '127.0.0.1:8000',
        'transit_key': {
            'id': crypto.b64encode(response_transit_key['id']),
            'key': crypto.b64encode(response_transit_key['key']),
            'type': response_transit_key['type']
        },
        'created_at': t_ms
    }

    # Sign the request
    private_key = peer.get_private_key(from_peer_id, db)
    signed_request = crypto.sign_event(request_data, private_key)

    # Wrap with recipient's prekey
    to_key = prekey.get_transit_prekey_for_peer(to_peer_id, db)
    canonical = crypto.canonicalize_json(signed_request)
    request_blob = crypto.wrap(canonical, to_key, db)

    # simulate sending - add to incoming queue
    queues.incoming.add(request_blob, t_ms, db)


def receive_request(request_blob: bytes, db: Any) -> dict[str, Any] | None:
    """Receive and verify a sync request. Returns request data if valid, None otherwise."""
    # Unwrap the request
    unwrapped = crypto.unwrap(request_blob, db)
    if not unwrapped:
        return None

    # Parse JSON
    request_data = crypto.parse_json(unwrapped)

    # Verify signature - get public key from peer_id
    peer_id = request_data.get('peer_id')
    public_key = peer.get_public_key(peer_id, db)
    if not crypto.verify_event(request_data, public_key):
        return None  # Reject unsigned or invalid signature

    return request_data


def project(sync_event_id: str, seen_by_peer_id: str, received_at: int, db: Any) -> None:
    """Project sync event by sending a response with all shareable events.

    Args:
        sync_event_id: ID of the sync request event
        seen_by_peer_id: Peer who received the sync request
        received_at: Timestamp when sync request was received
        db: Database connection
    """
    import logging
    log = logging.getLogger(__name__)
    log.info(f"sync.project() called for event {sync_event_id} seen by {seen_by_peer_id}")

    # Get the sync event data from store
    sync_blob = store.get(sync_event_id, db)
    if not sync_blob:
        log.info(f"sync blob not found in store")
        return

    # Parse sync request data
    sync_data = crypto.parse_json(sync_blob)

    # Extract requester info
    requester_peer_id = sync_data.get('peer_id')
    requester_peer_shared_id = sync_data.get('peer_shared_id')
    transit_key_encoded = sync_data.get('transit_key')

    if not requester_peer_id or not requester_peer_shared_id or not transit_key_encoded:
        return  # Invalid sync request

    # Only respond to sync requests from peers we recognize (have their peer_shared)
    requester_known = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND seen_by_peer_id = ?",
        (requester_peer_shared_id, seen_by_peer_id)
    )
    if not requester_known:
        log.info(f"Rejecting sync request from unrecognized peer {requester_peer_shared_id}")
        return

    log.info(f"Accepting sync request from recognized peer {requester_peer_shared_id}")

    # Decode transit_key from base64-encoded dict to bytes dict
    transit_key_dict = {
        'id': crypto.b64decode(transit_key_encoded['id']),
        'key': crypto.b64decode(transit_key_encoded['key']),
        'type': transit_key_encoded['type']
    }

    # Send response with all shareable events (now using requester_peer_shared_id directly)
    send_response(requester_peer_id, requester_peer_shared_id, seen_by_peer_id, transit_key_dict, received_at, db)


def send_response(to_peer_id: str, to_peer_shared_id: str, from_peer_id: str, transit_key_dict: dict[str, Any], t_ms: int, db: Any) -> None:
    """Send a sync response containing all shareable events seen by from_peer_id.

    Args:
        to_peer_id: Requester's peer_id (for logging)
        to_peer_shared_id: Requester's peer_shared_id (which events to send - events NOT created by requester)
        from_peer_id: Responder's peer_id (which peer is sending the response)
        transit_key_dict: Transit key dict from the sync request (format: {'id': bytes, 'key': bytes, 'type': 'symmetric'})
        t_ms: Current timestamp
        db: Database connection
    """
    import logging
    log = logging.getLogger(__name__)

    # Get responder's own peer_shared_id deterministically
    # peers_shared may contain multiple rows (self + others) for this seen_by_peer_id,
    # so we must select the row whose underlying event references from_peer_id.
    from_peer_shared_id = ""
    candidate_rows = db.query(
        "SELECT peer_shared_id FROM peers_shared WHERE seen_by_peer_id = ?",
        (from_peer_id,)
    )
    for row in candidate_rows:
        ps_id = row['peer_shared_id']
        try:
            ps_blob = store.get(ps_id, db)
            if not ps_blob:
                continue
            ps_data = crypto.parse_json(ps_blob)
            if ps_data.get('type') == 'peer_shared' and ps_data.get('peer_id') == from_peer_id:
                from_peer_shared_id = ps_id
                break
        except Exception:
            continue

    if not from_peer_shared_id:
        log.info(f"No self peer_shared_id found for responder {from_peer_id}")
        return  # Can't determine responder's shareable identity

    log.info(f"Responder {from_peer_id} has peer_shared_id {from_peer_shared_id}")
    log.info(f"Querying shareable events WHERE peer_id={from_peer_shared_id} AND peer_id!={to_peer_shared_id}")

    # Query all shareable events created by the responder (using their peer_shared_id)
    # These are the events the requester doesn't have (events NOT created by requester)
    shareable_rows = db.query(
        """SELECT event_id FROM shareable_events
           WHERE peer_id = ? AND peer_id != ?
           ORDER BY created_at ASC""",
        (from_peer_shared_id, to_peer_shared_id)
    )

    log.info(f"Found {len(shareable_rows)} shareable events to send")

    # For each shareable event, double-wrap it with transit key (keep original wrapping)
    for row in shareable_rows:
        event_id = row['event_id']
        # Get the event blob from store
        event_blob = store.get(event_id, db)
        if not event_blob:
            continue  # Skip if blob not found

        # Double-wrap: wrap the already-wrapped event blob with transit key for transport
        # Recipient will unwrap twice: first with transit key, then with original key
        wrapped_blob = crypto.wrap(event_blob, transit_key_dict, db)

        # Add to incoming queue (will be received by requester on next drain)
        queues.incoming.add(wrapped_blob, t_ms, db)
