"""Debug script to check sync_connections."""
import sqlite3
from db import Database, create_unsafe_db
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

# Check sync_connections before any ticks
unsafedb = create_unsafe_db(db)
print("\n=== Before any ticks ===")
connections = unsafedb.query("SELECT peer_shared_id, last_seen_ms, ttl_ms FROM sync_connections")
print(f"Sync connections: {len(connections)}")
for conn in connections:
    print(f"  peer_shared_id={conn['peer_shared_id'][:20]}... last_seen={conn['last_seen_ms']} ttl={conn['ttl_ms']}")

# Do sync rounds
for i in range(5):
    print(f"\n=== Sync round {i+1} (t_ms={4000 + i*200}) ===")
    tick.tick(t_ms=4000 + i*200, db=db)

    connections = unsafedb.query("SELECT peer_shared_id, last_seen_ms, ttl_ms FROM sync_connections")
    print(f"Sync connections: {len(connections)}")
    for conn in connections:
        print(f"  peer_shared_id={conn['peer_shared_id'][:20]}... last_seen={conn['last_seen_ms']} ttl={conn['ttl_ms']}")
        if conn['peer_shared_id'] == alice['peer_shared_id']:
            print(f"    *** ALICE! ***")
        if conn['peer_shared_id'] == bob['peer_shared_id']:
            print(f"    *** BOB! ***")
