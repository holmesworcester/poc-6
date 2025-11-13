"""Debug script to check if Bob has Alice's keys."""
import sqlite3
from db import Database, create_safe_db
import schema
from events.identity import user, invite, peer
import tick

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates a network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id'][:20]}...")
print(f"Alice key_id (all_users group key): {alice['key_id'][:20]}...")

# Alice creates an invite for Bob
invite_id, invite_link, invite_data = invite.create(
    peer_id=alice['peer_id'],
    t_ms=1500,
    db=db
)

# Bob joins Alice's network
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)
bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"\nBob peer_id: {bob['peer_id'][:20]}...")

db.commit()

# Sync
print("\n=== Syncing ===")
for i in range(15):
    tick.tick(t_ms=4000 + i*200, db=db)

# Check if Bob has Alice's key
bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
bob_has_alice_key = bob_safedb.query_one(
    "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
    (alice['key_id'], bob['peer_id'])
)
print(f"\nBob has Alice's group key: {bool(bob_has_alice_key)}")

# Check all keys Bob has
bob_keys = bob_safedb.query("SELECT key_id FROM group_keys WHERE recorded_by = ?", (bob['peer_id'],))
print(f"\nBob has {len(bob_keys)} group keys:")
for key in bob_keys:
    print(f"  - {key['key_id'][:20]}...")
    if key['key_id'] == alice['key_id']:
        print(f"    *** THIS IS ALICE'S KEY! ***")

# Check which keys Alice shared with Bob
alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
alice_key_shares = alice_safedb.query("SELECT * FROM group_key_shared WHERE recorded_by = ?", (alice['peer_id'],))
print(f"\nAlice created {len(alice_key_shares)} group_key_shared events:")
for share in alice_key_shares:
    print(f"  - key_id={share['wrapped_key_id'][:20]}... wrapped_in={share['key_shared_id'][:20]}...")
