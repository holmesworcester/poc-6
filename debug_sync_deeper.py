"""Debug: trace what happens when we receive sync requests."""
import sqlite3
import logging
logging.basicConfig(level=logging.DEBUG, format='%(name)s - %(levelname)s: %(message)s')

from db import Database
import schema
from events import peer, key
from events import sync as sync_module
import crypto
import store

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice and Bob
alice_peer_id, alice_peer_shared_id = peer.create(t_ms=1000, db=db)
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)

# Register prekeys
alice_public_key = peer.get_public_key(alice_peer_id, db)
bob_public_key = peer.get_public_key(bob_peer_id, db)

db.execute(
    "INSERT INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
    (bob_peer_id, bob_public_key, 4000)
)
db.commit()

# Alice sends sync request to Bob
print("\n=== Alice sends sync request to Bob ===")
sync_module.send_request(to_peer_id=bob_peer_id, from_peer_id=alice_peer_id, from_peer_shared_id=alice_peer_shared_id, t_ms=18000, db=db)

# Check incoming queue
blobs = db.query("SELECT blob FROM incoming_blobs", ())
print(f"Incoming queue has {len(blobs)} blob(s)")

# Manually unwrap to see what we get
blob = blobs[0]['blob']
print(f"\nManually unwrapping blob...")
hint = sync_module.key.extract_id(blob)
hint_b64 = crypto.b64encode(hint)
print(f"Hint: {hint_b64}")

recorded_by = sync_module.key.get_peer_id_for_key(hint_b64, db)
print(f"Seen by peer: {recorded_by}")

unwrapped_blob, missing_keys = crypto.unwrap(blob, db)
if unwrapped_blob:
    event_data = crypto.parse_json(unwrapped_blob)
    print(f"Unwrapped event type: {event_data.get('type')}")
    print(f"Full event data: {event_data}")

    # Store it
    event_id = store.blob(unwrapped_blob, 22000, True, db)
    print(f"\nStored event with ID: {event_id}")

    # Create recorded
    from events import recorded
    recorded_id = recorded.create(event_id, recorded_by, 22000, db, True)
    print(f"Created recorded with ID: {recorded_id}")

    # Get the recorded blob
    fs_blob = store.get(recorded_id, db)
    if fs_blob:
        fs_data = crypto.parse_json(fs_blob)
        print(f"First_seen data: {fs_data}")

    # Now project it
    print(f"\nProjecting recorded event...")
    result = recorded.project(recorded_id, db)
    print(f"Project result: {result}")

    # Check incoming queue again
    incoming_count = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs", ())
    print(f"\nIncoming queue now has {incoming_count['cnt']} blobs")
else:
    print(f"Failed to unwrap: missing_keys={missing_keys}")
