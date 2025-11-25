"""Check if Device 1 knows about Device 2 (peer discovery)."""
import sqlite3
from db import Database
import schema
from events.identity import user, link_invite, link
import tick

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
device1_peer_id = alice_device1['peer_id']
device1_peer_shared_id = alice_device1['peer_shared_id']

db.commit()

invite_id, invite_link, invite_data = link_invite.create(
    peer_id=device1_peer_id,
    t_ms=2000,
    db=db
)

db.commit()

alice_device2 = link.join(
    link_url=invite_link,
    t_ms=3000,
    db=db
)
device2_peer_id = alice_device2['peer_id']
device2_peer_shared_id = alice_device2['peer_shared_id']

db.commit()

print(f"\nDevice 1: peer_shared_id={device1_peer_shared_id[:10]}...")
print(f"Device 2: peer_shared_id={device2_peer_shared_id[:10]}...")

print(f"\n=== BEFORE ticks ===")

# Check peers_shared table
print(f"\npeers_shared table:")
d1_peers = db.query(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (device1_peer_id,)
)
print(f"Device 1 knows about {len(d1_peers)} peer(s):")
for p in d1_peers:
    peer_match = "Device 1" if p['peer_shared_id'] == device1_peer_shared_id else ("Device 2" if p['peer_shared_id'] == device2_peer_shared_id else "Unknown")
    print(f"  {p['peer_shared_id'][:10]}... ({peer_match})")

d2_peers = db.query(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (device2_peer_id,)
)
print(f"Device 2 knows about {len(d2_peers)} peer(s):")
for p in d2_peers:
    peer_match = "Device 1" if p['peer_shared_id'] == device1_peer_shared_id else ("Device 2" if p['peer_shared_id'] == device2_peer_shared_id else "Unknown")
    print(f"  {p['peer_shared_id'][:10]}... ({peer_match})")

# Check linked_peers table
print(f"\nlinked_peers table:")
d1_linked = db.query(
    "SELECT peer_id, user_id FROM linked_peers WHERE recorded_by = ?",
    (device1_peer_id,)
)
print(f"Device 1 sees {len(d1_linked)} linked peer(s):")
for p in d1_linked:
    peer_match = "Device 1" if p['peer_id'] == device1_peer_shared_id else ("Device 2" if p['peer_id'] == device2_peer_shared_id else "Unknown")
    print(f"  peer_id={p['peer_id'][:10]}... ({peer_match}) user_id={p['user_id'][:10]}...")

d2_linked = db.query(
    "SELECT peer_id, user_id FROM linked_peers WHERE recorded_by = ?",
    (device2_peer_id,)
)
print(f"Device 2 sees {len(d2_linked)} linked peer(s):")
for p in d2_linked:
    peer_match = "Device 1" if p['peer_id'] == device1_peer_shared_id else ("Device 2" if p['peer_id'] == device2_peer_shared_id else "Unknown")
    print(f"  peer_id={p['peer_id'][:10]}... ({peer_match}) user_id={p['user_id'][:10]}...")

# Run a few ticks
for i in range(1, 4):
    print(f"\n=== Tick #{i} ===")
    tick.tick(t_ms=3000 + i*1000, db=db)

print(f"\n=== AFTER ticks ===")

# Check again
d1_peers = db.query(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (device1_peer_id,)
)
print(f"\nDevice 1 now knows about {len(d1_peers)} peer(s):")
for p in d1_peers:
    peer_match = "Device 1" if p['peer_shared_id'] == device1_peer_shared_id else ("Device 2" if p['peer_shared_id'] == device2_peer_shared_id else "Unknown")
    print(f"  {p['peer_shared_id'][:10]}... ({peer_match})")

# Check if Device 1 knows Device 2 via ANY discovery mechanism
# (peers_shared, linked_peers, invite_accepteds, or sync_connections)
knows_via_peers_shared = any(p['peer_shared_id'] == device2_peer_shared_id for p in d1_peers)
knows_via_linked = any(p['peer_id'] == device2_peer_shared_id for p in d1_linked)

# Check sync_connections table (device-wide, no recorded_by)
connections = db.query("SELECT peer_shared_id FROM sync_connections")
print(f"\nsync_connections table has {len(connections)} entry/entries:")
for conn in connections:
    peer_match = "Device 1" if conn['peer_shared_id'] == device1_peer_shared_id else ("Device 2" if conn['peer_shared_id'] == device2_peer_shared_id else "Unknown")
    print(f"  {conn['peer_shared_id'][:10]}... ({peer_match})")

knows_via_connections = any(conn['peer_shared_id'] == device2_peer_shared_id for conn in connections)

print(f"\n=== Summary ===")
print(f"Device 1 knows about Device 2 via peers_shared: {knows_via_peers_shared}")
print(f"Device 1 knows about Device 2 via linked_peers: {knows_via_linked}")
print(f"Device 2 is in sync_connections: {knows_via_connections}")

if knows_via_connections:
    print(f"\n✅ Device 2 IS in sync_connections!")
    print(f"   Device 1 should be sending sync requests to Device 2")
elif not (knows_via_peers_shared or knows_via_linked):
    print(f"\n❌ Device 1 doesn't know about Device 2!")
    print(f"   This is why Device 1 isn't sending sync requests to Device 2")
else:
    print(f"\n✅ Device 1 DOES know about Device 2")
    print(f"   The sync issue must be elsewhere")
