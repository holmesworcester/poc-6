"""Debug script to check if channel is in shareable_events."""
import sqlite3
from db import Database, create_safe_db, create_unsafe_db
import schema
from events.identity import user, invite, peer
import tick
import store
import crypto

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates a network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id'][:20]}...")
print(f"Alice channel_id: {alice['channel_id'][:20]}...")

# Check if channel is in Alice's shareable_events
alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
channel_shareable = alice_safedb.query_one(
    "SELECT * FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
    (alice['channel_id'], alice['peer_id'])
)
print(f"\nChannel in Alice's shareable_events: {bool(channel_shareable)}")

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
print(f"Bob peer_shared_id: {bob['peer_shared_id'][:20]}...")

db.commit()

# Check sync connections before sync
print("\n=== Before sync ===")
unsafedb = create_unsafe_db(db)
connections_before = unsafedb.query("SELECT peer_shared_id FROM sync_connections")
print(f"Sync connections: {len(connections_before)}")
for conn in connections_before:
    print(f"  - {conn['peer_shared_id'][:20]}...")

# Sync
for i in range(5):
    print(f"\n=== Sync Round {i+1} ===")
    tick.tick(t_ms=4000 + i*200, db=db)

    # Check sync connections after each round
    connections = unsafedb.query("SELECT peer_shared_id FROM sync_connections")
    print(f"Sync connections: {len(connections)}")
    for conn in connections:
        print(f"  - {conn['peer_shared_id'][:20]}...")

    # Check if Bob has Alice's peer_shared
    bob_has_alice = alice_safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob['peer_shared_id'], alice['peer_id'])
    )
    print(f"Alice knows Bob's peer_shared: {bool(bob_has_alice)}")

    bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
    alice_has_bob = bob_safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (alice['peer_shared_id'], bob['peer_id'])
    )
    print(f"Bob knows Alice's peer_shared: {bool(alice_has_bob)}")

# Check shareable events
print("\n=== Checking shareable events ===")
alice_shareable = alice_safedb.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],))
print(f"\nAlice has {len(alice_shareable)} shareable events:")
for row in alice_shareable:
    event_id = row['event_id']
    try:
        blob = store.get(event_id, db)
        if blob:
            data = crypto.parse_json(blob)
            event_type = data.get('type', 'unknown')
            print(f"  - {event_id[:20]}... type={event_type}")
            if event_type == 'channel':
                print(f"    *** THIS IS THE CHANNEL! ***")
    except:
        print(f"  - {event_id[:20]}... (encrypted or error)")
