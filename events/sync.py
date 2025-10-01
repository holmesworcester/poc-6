from typing import Any
import seen
import incoming
import crypto
import key
import prekey
import store
import network


def receive(batch_size, t_ms, db):
    transit_blobs = incoming.drain(batch_size)
    new_seen_ids = transit_blobs.map(crypto.unwrap_and_store, t_ms, db)
    seen.project_ids(new_seen_ids, db)
    db.commit();
    return None

def send_requests(from_peer_id, db):
    peer_ids = db.query("SELECT peer_id FROM peers WHERE peer_id != ?", (from_peer_id,))
    for to_peer_id in peer_ids:
        send_request(to_peer_id, from_peer_id, db)
    db.commit()
    return None

def send_request(to_peer_id, from_peer_id, db):
    """Send a sync request to a peer."""
    # Create a sync event for the peer
    response_transit_key = key.create_sym_key(db)
    request_plaintext = {'type': "sync", 'peer_id': from_peer_id, 'address': "127.0.0.1:8000", 'transit_key': response_transit_key}
    to_key = prekey.get_transit_prekey_for_peer(to_peer_id, db)
    request_blob = crypto.wrap(request_plaintext, to_key, db)

    # simulate sending 
    incoming.create(request_blob, db)
    sync_plaintext = create(to_peer_id, from_peer_id, db)
    incoming.create(peer_ids, request_blob, db)
    return None