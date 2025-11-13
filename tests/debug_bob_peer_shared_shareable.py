"""Debug script to check if Bob's peer_shared is shareable."""
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

# Check if Bob's peer_shared is in Bob's shareable_events
bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
bob_peer_shared_shareable = bob_safedb.query_one(
    "SELECT * FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
    (bob['peer_shared_id'], bob['peer_id'])
)
print(f"\nBob's peer_shared in Bob's shareable_events: {bool(bob_peer_shared_shareable)}")

# Check LOCAL_ONLY_TYPES
print("\n'peer_shared' in LOCAL_ONLY_TYPES?")
from events.transit import recorded
LOCAL_ONLY_TYPES = {'peer', 'transit_key', 'group_key', 'transit_prekey', 'group_prekey', 'recorded', 'network_joined', 'invite_accepted', 'sync_connect'}
print(f"  'peer_shared' in LOCAL_ONLY: {'peer_shared' in LOCAL_ONLY_TYPES}")
print(f"  'peer' in LOCAL_ONLY: {'peer' in LOCAL_ONLY_TYPES}")

# Do one sync round
print("\n=== Sync round 1 ===")
tick.tick(t_ms=4000, db=db)

# Check if Bob sends his peer_shared to Alice
alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
alice_has_bob_peer_shared = alice_safedb.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (bob['peer_shared_id'], alice['peer_id'])
)
print(f"Alice has Bob's peer_shared after sync: {bool(alice_has_bob_peer_shared)}")
