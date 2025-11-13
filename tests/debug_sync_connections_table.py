"""Check what's in sync_connections table during multi-device linking."""
import sqlite3
from db import Database, create_unsafe_db
import schema
from events.identity import user, link_invite, link
import tick

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates network on phone
alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Phone peer_id: {alice_phone['peer_id'][:20]}...")
print(f"Phone peer_shared_id: {alice_phone['peer_shared_id'][:20]}...")

db.commit()

# Create link invite
link_invite_id, link_url, link_data = link_invite.create(
    peer_id=alice_phone['peer_id'],
    t_ms=2000,
    db=db
)

db.commit()

# Laptop joins
alice_laptop = link.join(link_url=link_url, t_ms=3000, db=db)
print(f"Laptop peer_id: {alice_laptop['peer_id'][:20]}...")
print(f"Laptop peer_shared_id: {alice_laptop['peer_shared_id'][:20]}...")

db.commit()

# Run one tick
print("\n=== After 1 tick ===")
tick.tick(t_ms=4000, db=db)

# Check sync_connections
unsafedb = create_unsafe_db(db)
connections = unsafedb.query("SELECT * FROM sync_connections")
print(f"Sync connections: {len(connections)}")
for conn in connections:
    peer_shared_id = conn['peer_shared_id']
    print(f"  peer_shared_id: {peer_shared_id[:20]}...")
    if peer_shared_id == alice_phone['peer_shared_id']:
        print(f"    (PHONE!)")
    elif peer_shared_id == alice_laptop['peer_shared_id']:
        print(f"    (LAPTOP!)")
    else:
        print(f"    (UNKNOWN)")

# Run more ticks
print("\n=== After 5 ticks ===")
for i in range(4):
    tick.tick(t_ms=4100 + i*100, db=db)

connections = unsafedb.query("SELECT * FROM sync_connections")
print(f"Sync connections: {len(connections)}")
for conn in connections:
    peer_shared_id = conn['peer_shared_id']
    print(f"  peer_shared_id: {peer_shared_id[:20]}...")
    if peer_shared_id == alice_phone['peer_shared_id']:
        print(f"    (PHONE!)")
    elif peer_shared_id == alice_laptop['peer_shared_id']:
        print(f"    (LAPTOP!)")
    else:
        print(f"    (UNKNOWN)")
