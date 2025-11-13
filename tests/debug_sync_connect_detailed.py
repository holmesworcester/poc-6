"""Debug script to check sync_connect in detail."""
import sqlite3
from db import Database, create_unsafe_db, create_safe_db
import schema
from events.identity import user, invite, peer
from events.transit import sync_connect
import logging

# Enable logging
logging.basicConfig(level=logging.INFO)

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates a network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id'][:20]}...")
print(f"Alice peer_shared_id: {alice['peer_shared_id'][:20]}...")

# Alice creates an invite for Bob
invite_id, invite_link, invite_data = invite.create(
    peer_id=alice['peer_id'],
    t_ms=1500,
    db=db
)

# Bob joins Alice's network
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)
bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"Bob peer_id: {bob['peer_id'][:20]}...")
print(f"Bob peer_shared_id: {bob['peer_shared_id'][:20]}...")

db.commit()

# Manually call send_connect_to_all BEFORE any sync
print("\n=== Calling sync_connect.send_connect_to_all at t_ms=3000 ===")
sync_connect.send_connect_to_all(t_ms=3000, db=db)

# Process incoming blobs (includes sync_connect events)
print("\n=== Calling sync.receive() to process sync_connect events ===")
from events.transit import sync
sync.receive(batch_size=20, t_ms=3000, db=db)

# Check sync_connections
unsafedb = create_unsafe_db(db)
connections = unsafedb.query("SELECT peer_shared_id, last_seen_ms FROM sync_connections")
print(f"Sync connections after sync.receive(): {len(connections)}")
for conn in connections:
    print(f"  peer_shared_id={conn['peer_shared_id'][:20]}... last_seen={conn['last_seen_ms']}")
    if conn['peer_shared_id'] == alice['peer_shared_id']:
        print(f"    *** ALICE! ***")
    if conn['peer_shared_id'] == bob['peer_shared_id']:
        print(f"    *** BOB! ***")

# Check what Alice sees before sending
print("\n=== Before 2nd send: What does Alice see? ===")
alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
alice_peers_shared = alice_safedb.query("SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?", (alice['peer_id'],))
print(f"Alice peers_shared: {len(alice_peers_shared)}")
for row in alice_peers_shared:
    print(f"  - {row['peer_shared_id'][:20]}...")

alice_bootstrap = alice_safedb.query("SELECT inviter_peer_shared_id FROM invite_accepteds WHERE recorded_by = ?", (alice['peer_id'],))
print(f"Alice bootstrap (invite_accepteds): {len(alice_bootstrap)}")

connections_for_alice = unsafedb.query("SELECT peer_shared_id FROM sync_connections WHERE last_seen_ms + ttl_ms > ?", (3100,))
print(f"Connections (device-wide, not filtered by peer): {len(connections_for_alice)}")
for row in connections_for_alice:
    peer_shared_id = row['peer_shared_id']
    print(f"  - {peer_shared_id[:20]}...")
    if peer_shared_id == bob['peer_shared_id']:
        print(f"    THIS IS BOB!")
    if peer_shared_id == alice['peer_shared_id']:
        print(f"    THIS IS ALICE!")

# Now manually call send_connect_to_all AGAIN (after Bob sent his)
print("\n=== Calling sync_connect.send_connect_to_all at t_ms=3100 (after Bob's connect arrived) ===")
sync_connect.send_connect_to_all(t_ms=3100, db=db)

print("\n=== Calling sync.receive() again to process Alice's sync_connect ===")
sync.receive(batch_size=20, t_ms=3100, db=db)

connections = unsafedb.query("SELECT peer_shared_id, last_seen_ms FROM sync_connections")
print(f"Sync connections after 2nd cycle: {len(connections)}")
for conn in connections:
    print(f"  peer_shared_id={conn['peer_shared_id'][:20]}... last_seen={conn['last_seen_ms']}")
    if conn['peer_shared_id'] == alice['peer_shared_id']:
        print(f"    *** ALICE! ***")
    if conn['peer_shared_id'] == bob['peer_shared_id']:
        print(f"    *** BOB! ***")
