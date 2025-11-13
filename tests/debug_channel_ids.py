"""Debug script to check channel IDs."""
import sqlite3
from db import Database
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
print(f"Alice channel_id: {alice['channel_id'][:20]}...")

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
print(f"Bob channel_id from join(): {bob['channel_id'][:20]}...")

print(f"\nAre channel IDs the same? {alice['channel_id'] == bob['channel_id']}")

db.commit()

# Sync so Bob gets Alice's channel event
for i in range(15):
    tick.tick(t_ms=4000 + i*200, db=db)

# Check if channel_id is valid for Bob
from db import create_safe_db
bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
channel_valid = bob_safedb.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (bob['channel_id'], bob['peer_id'])
)
print(f"Bob has valid channel after sync: {bool(channel_valid)}")

# Check if channel exists in Bob's channels table
channel_row = bob_safedb.query_one(
    "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
    (bob['channel_id'], bob['peer_id'])
)
print(f"Bob has channel in channels table: {bool(channel_row)}")
if channel_row:
    print(f"  group_id: {channel_row['group_id'][:20]}...")
