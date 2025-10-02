from typing import Any
from events import first_seen, incoming, key, prekey, network, peer_secret
import crypto
import store
import json


def receive(batch_size: int, t_ms: int, db: Any) -> None:
    """Receive and process a batch of incoming transit blobs."""
    transit_blobs = incoming.drain(batch_size, db)
    new_first_seen_ids = [crypto.unwrap_and_store(blob, t_ms, db) for blob in transit_blobs]
    first_seen.project_ids(new_first_seen_ids, db)
    db.commit()


def send_requests(from_peer_id: str, t_ms: int, db: Any) -> None:
    """Send sync requests to all peers."""
    peer_rows = db.query("SELECT peer_id FROM peers WHERE peer_id != ?", (from_peer_id,))
    for row in peer_rows:
        send_request(row['peer_id'], from_peer_id, t_ms, db)
    db.commit()

def send_request(to_peer_id: str, from_peer_id: str, t_ms: int, db: Any) -> None:
    """Send a sync request to a peer."""
    # Create a sync event for the peer
    response_transit_key = key.create_sym_key(db)
    request_data = {
        'type': 'sync',
        'peer_id': from_peer_id,
        'address': '127.0.0.1:8000',
        'transit_key': response_transit_key,
        'created_at': t_ms
    }

    # Sign the request
    private_key = peer_secret.get_private_key(from_peer_id, db)
    signed_request = crypto.sign_event(request_data, private_key)

    # Wrap with recipient's prekey
    to_key = prekey.get_transit_prekey_for_peer(to_peer_id, db)
    request_blob = crypto.wrap(signed_request, to_key, db)

    # simulate sending - add to incoming queue
    incoming.create(request_blob, t_ms, db)
    # TODO: create sync response and send it back


def receive_request(request_blob: bytes, db: Any) -> dict[str, Any] | None:
    """Receive and verify a sync request. Returns request data if valid, None otherwise."""
    # Unwrap the request
    unwrapped = crypto.unwrap(request_blob, db)
    if not unwrapped:
        return None

    # Parse JSON
    request_data = json.loads(unwrapped.decode() if isinstance(unwrapped, bytes) else unwrapped)

    # Verify signature - get public key from peer_id
    peer_id = request_data.get('peer_id')
    public_key = peer_secret.get_public_key(peer_id, db)
    if not crypto.verify_event(request_data, public_key):
        return None  # Reject unsigned or invalid signature

    return request_data