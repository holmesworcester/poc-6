"""Debug script to check if group_key_shared events are shareable."""
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
print(f"Alice key_id: {alice['key_id'][:20]}...")

# Check if any group_key_shared events exist for Alice
cursor = db._conn.execute("SELECT event_id FROM events")
all_events = [{'event_id': row[0]} for row in cursor.fetchall()]
print(f"\nTotal events in DB: {len(all_events)}")

gks_events = []
for event_row in all_events:
    event_id = event_row['event_id']
    try:
        blob = store.get(event_id, db)
        if blob:
            data = crypto.parse_json(blob)
            if data.get('type') == 'group_key_shared':
                gks_events.append(event_id)
                print(f"Found group_key_shared event: {event_id[:20]}...")
    except:
        pass

print(f"\nTotal group_key_shared events: {len(gks_events)}")

# Check if they're in shareable_events
alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
for gks_id in gks_events:
    shareable = alice_safedb.query_one(
        "SELECT * FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (gks_id, alice['peer_id'])
    )
    print(f"  {gks_id[:20]}... in shareable? {bool(shareable)}")

# Now add Bob and sync
invite_id, invite_link, invite_data = invite.create(
    peer_id=alice['peer_id'],
    t_ms=1500,
    db=db
)
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)
bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"\nBob peer_id: {bob['peer_id'][:20]}...")

db.commit()

# Sync once
print("\n=== After 1 sync ===")
tick.tick(t_ms=4000, db=db)

# Re-check group_key_shared events
cursor_after = db._conn.execute("SELECT event_id FROM events")
all_events_after = [{'event_id': row[0]} for row in cursor_after.fetchall()]
gks_events_after = []
for event_row in all_events_after:
    event_id = event_row['event_id']
    try:
        blob = store.get(event_id, db)
        if blob:
            data = crypto.parse_json(blob)
            if data.get('type') == 'group_key_shared':
                gks_events_after.append(event_id)
    except:
        pass

print(f"Total group_key_shared events after Bob joined: {len(gks_events_after)}")

# Check if new GKS events are shareable by Alice
for gks_id in gks_events_after:
    if gks_id not in gks_events:
        print(f"NEW GKS event: {gks_id[:20]}...")
        shareable = alice_safedb.query_one(
            "SELECT * FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
            (gks_id, alice['peer_id'])
        )
        print(f"  in Alice shareable? {bool(shareable)}")
