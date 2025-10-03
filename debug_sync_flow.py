"""Debug sync flow to understand what's happening."""
import sqlite3
import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

from db import Database
import schema
from events import peer, key, group, channel, message, sync
import crypto

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
db.execute(
    "INSERT INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)",
    (alice_peer_id, alice_public_key, 5000)
)
db.commit()

# Alice's setup
alice_key_id = key.create(alice_peer_id, t_ms=6000, db=db)
alice_group_id = group.create(
    name='Alice Group',
    peer_id=alice_peer_id,
    peer_shared_id=alice_peer_shared_id,
    key_id=alice_key_id,
    t_ms=7000,
    db=db
)
alice_channel_id = channel.create(
    name='general',
    group_id=alice_group_id,
    peer_id=alice_peer_id,
    peer_shared_id=alice_peer_shared_id,
    key_id=alice_key_id,
    t_ms=8000,
    db=db
)

# Bob's setup
bob_key_id = key.create(bob_peer_id, t_ms=9000, db=db)
bob_group_id = group.create(
    name='Bob Group',
    peer_id=bob_peer_id,
    peer_shared_id=bob_peer_shared_id,
    key_id=bob_key_id,
    t_ms=10000,
    db=db
)
bob_channel_id = channel.create(
    name='general',
    group_id=bob_group_id,
    peer_id=bob_peer_id,
    peer_shared_id=bob_peer_shared_id,
    key_id=bob_key_id,
    t_ms=11000,
    db=db
)

# Create messages
alice_msg = message.create_message(
    params={
        'content': 'Hello from Alice',
        'channel_id': alice_channel_id,
        'group_id': alice_group_id,
        'peer_id': alice_peer_id,
        'peer_shared_id': alice_peer_shared_id,
        'key_id': alice_key_id
    },
    t_ms=15000,
    db=db
)

bob_msg = message.create_message(
    params={
        'content': 'Hello from Bob',
        'channel_id': bob_channel_id,
        'group_id': bob_group_id,
        'peer_id': bob_peer_id,
        'peer_shared_id': bob_peer_shared_id,
        'key_id': bob_key_id
    },
    t_ms=16000,
    db=db
)

print("\n=== Initial State ===")
print(f"Alice's shareable events:")
alice_shareable = db.query(
    "SELECT event_id, peer_id FROM shareable_events WHERE peer_id = ?",
    (alice_peer_shared_id,)
)
for row in alice_shareable:
    print(f"  {row['event_id']} (peer_id={row['peer_id']})")

print(f"\nBob's shareable events:")
bob_shareable = db.query(
    "SELECT event_id, peer_id FROM shareable_events WHERE peer_id = ?",
    (bob_peer_shared_id,)
)
for row in bob_shareable:
    print(f"  {row['event_id']} (peer_id={row['peer_id']})")

# Send sync requests
print("\n=== Sending Sync Requests ===")
sync.send_request(to_peer_id=bob_peer_id, from_peer_id=alice_peer_id, from_peer_shared_id=alice_peer_shared_id, t_ms=18000, db=db)
print(f"Alice sent request to Bob")

sync.send_request(to_peer_id=alice_peer_id, from_peer_id=bob_peer_id, from_peer_shared_id=bob_peer_shared_id, t_ms=19000, db=db)
print(f"Bob sent request to Alice")

# Check incoming queue
incoming_count = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs", ())
print(f"\nIncoming queue has {incoming_count['cnt']} blobs")

# Round 1: Receive sync requests
print("\n=== Round 1: Receiving Sync Requests ===")
sync.receive(batch_size=10, t_ms=22000, db=db)

# Check what happened
incoming_count = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs", ())
print(f"Incoming queue now has {incoming_count['cnt']} blobs (should have responses)")

# Check valid events
alice_valid = db.query("SELECT event_id FROM valid_events WHERE recorded_by = ?", (alice_peer_id,))
bob_valid = db.query("SELECT event_id FROM valid_events WHERE recorded_by = ?", (bob_peer_id,))
print(f"Alice has {len(alice_valid)} valid events")
print(f"Bob has {len(bob_valid)} valid events")

# Round 2: Receive sync responses
print("\n=== Round 2: Receiving Sync Responses ===")
sync.receive(batch_size=100, t_ms=23000, db=db)

# Check incoming queue
incoming_count = db.query_one("SELECT COUNT(*) as cnt FROM incoming_blobs", ())
print(f"Incoming queue now has {incoming_count['cnt']} blobs")

# Final check
alice_valid = db.query("SELECT event_id FROM valid_events WHERE recorded_by = ?", (alice_peer_id,))
bob_valid = db.query("SELECT event_id FROM valid_events WHERE recorded_by = ?", (bob_peer_id,))
print(f"Alice has {len(alice_valid)} valid events")
print(f"Bob has {len(bob_valid)} valid events")

# Check if messages are visible
bob_msg_for_alice = db.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (bob_msg['id'], alice_peer_id)
)
alice_msg_for_bob = db.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (alice_msg['id'], bob_peer_id)
)

print(f"\nBob's message visible to Alice: {bob_msg_for_alice is not None}")
print(f"Alice's message visible to Bob: {alice_msg_for_bob is not None}")
