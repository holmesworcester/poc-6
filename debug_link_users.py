"""Debug script to understand why linked device's users table is empty."""
import sqlite3
from db import Database
import schema
from events.identity import user, link_invite, link
from events.content import message
import tick

conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates network
alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice phone: peer_id={alice_phone['peer_id'][:20]}, user_id={alice_phone['user_id'][:20]}")
db.commit()

# Create link invite
link_invite_id, link_url, link_data = link_invite.create(
    peer_id=alice_phone['peer_id'],
    t_ms=2000,
    db=db
)
db.commit()

# Alice joins on laptop
alice_laptop = link.join(link_url=link_url, t_ms=3000, db=db)
print(f"Alice laptop: peer_id={alice_laptop['peer_id'][:20]}, user_id={alice_laptop['user_id'][:20]}")
print(f"  link_id={alice_laptop['link_id'][:20]}")
db.commit()

# Check users table for laptop
laptop_peer_id = alice_laptop['peer_id']
laptop_peer_shared = db.query_one(
    "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
    (laptop_peer_id, laptop_peer_id)
)
print(f"\nLaptop peer_shared_id: {laptop_peer_shared['peer_shared_id'][:20]}")

# Check all users for laptop's view
laptop_users = db.query_all(
    "SELECT user_id, peer_id, name FROM users WHERE recorded_by = ?",
    (laptop_peer_id,)
)
print(f"\nUsers visible to laptop ({len(laptop_users)}):")
for u in laptop_users:
    print(f"  user_id={u['user_id'][:20]}, peer_id={u['peer_id'][:20] if u['peer_id'] else 'None'}, name={u['name']}")

# Check if user record exists with laptop's peer_shared_id
user_row = db.query_one(
    "SELECT user_id, peer_id FROM users WHERE peer_id = ? AND recorded_by = ?",
    (laptop_peer_shared['peer_shared_id'], laptop_peer_id)
)
if user_row:
    print(f"\n✓ Found user record for laptop's peer_shared_id: user_id={user_row['user_id'][:20]}")
else:
    print(f"\n✗ No user record found for laptop's peer_shared_id: {laptop_peer_shared['peer_shared_id'][:20]}")

# Check linked_peers table
linked_peers = db.query_all(
    "SELECT link_id, user_id, peer_id FROM linked_peers WHERE recorded_by = ?",
    (laptop_peer_id,)
)
print(f"\nLinked peers visible to laptop ({len(linked_peers)}):")
for lp in linked_peers:
    print(f"  link_id={lp['link_id'][:20]}, user_id={lp['user_id'][:20]}, peer_id={lp['peer_id'][:20]}")

# Check valid_events for the link
link_valid = db.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (alice_laptop['link_id'], laptop_peer_id)
)
print(f"\nLink event valid: {bool(link_valid)}")

# Check all users in the db
all_users = db.query_all("SELECT user_id, peer_id, name, recorded_by FROM users")
print(f"\nAll users in DB ({len(all_users)}):")
for u in all_users:
    print(f"  user_id={u['user_id'][:20]}, peer_id={u['peer_id'][:20] if u['peer_id'] else 'None'}, recorded_by={u['recorded_by'][:20]}")
