#!/usr/bin/env python3
"""Debug sync peer_shared_id issues."""

import sqlite3
from db import Database
import schema
from events import peer, key, group, sync, prekey

# Setup: Initialize in-memory database
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create three peers
alice_peer_id, alice_peer_shared_id = peer.create(t_ms=1000, db=db)
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)
charlie_peer_id, charlie_peer_shared_id = peer.create(t_ms=3000, db=db)

print("Peers created:")
print(f"  Alice: peer_id={alice_peer_id[:16]}... peer_shared_id={alice_peer_shared_id[:16]}...")
print(f"  Bob: peer_id={bob_peer_id[:16]}... peer_shared_id={bob_peer_shared_id[:16]}...")
print(f"  Charlie: peer_id={charlie_peer_id[:16]}... peer_shared_id={charlie_peer_shared_id[:16]}...")

# Register prekeys: Alice and Bob know each other's public keys
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

# Send sync requests
print("\nSending sync requests:")
print(f"  Alice -> Bob with Alice's peer_shared_id={alice_peer_shared_id[:16]}...")
sync.send_request(to_peer_id=bob_peer_id, from_peer_id=alice_peer_id, from_peer_shared_id=alice_peer_shared_id, t_ms=6000, db=db)

print(f"  Bob -> Alice with Bob's peer_shared_id={bob_peer_shared_id[:16]}...")
sync.send_request(to_peer_id=alice_peer_id, from_peer_id=bob_peer_id, from_peer_shared_id=bob_peer_shared_id, t_ms=7000, db=db)

# Check what's in the incoming queue
incoming = db.query("SELECT blob FROM incoming_blobs")
print(f"\nIncoming queue has {len(incoming)} blobs")

# Process the sync requests
import logging
logging.basicConfig(level=logging.INFO, format='%(message)s')

print("\nProcessing sync requests (this triggers auto-responses):")
sync.receive(batch_size=10, t_ms=8000, db=db)

print("\n✓ Sync peer_shared_id debug complete")