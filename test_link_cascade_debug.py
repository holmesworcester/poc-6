"""Debug script to understand why linked device's users table is empty after sync."""
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

# Check what's in the link_data
print(f"\nLink data keys: {list(link_data.keys())}")
print(f"Has network_blob: {'network_blob' in link_data}")

# Alice joins on laptop
alice_laptop = link.join(link_url=link_url, t_ms=3000, db=db)
print(f"\nAlice laptop: peer_id={alice_laptop['peer_id'][:20]}, user_id={alice_laptop['user_id'][:20]}")
print(f"  link_id={alice_laptop['link_id'][:20]}")
db.commit()

laptop_peer_id = alice_laptop['peer_id']

# Check users table for laptop BEFORE sync
laptop_users_before = db.query_all(
    "SELECT user_id, peer_id, name FROM users WHERE recorded_by = ?",
    (laptop_peer_id,)
)
print(f"\nUsers visible to laptop BEFORE sync ({len(laptop_users_before)}):")
for u in laptop_users_before:
    print(f"  user_id={u['user_id'][:20]}, peer_id={u['peer_id'][:20] if u['peer_id'] else 'None'}, name={u['name']}")

# Check valid_events for laptop
valid_events = db.query_all(
    "SELECT event_id FROM valid_events WHERE recorded_by = ?",
    (laptop_peer_id,)
)
print(f"\nValid events for laptop ({len(valid_events)}):")
for v in valid_events:
    print(f"  {v['event_id'][:40]}...")

# Check what's blocked for laptop
blocked = db.query_all(
    "SELECT recorded_id, deps_remaining FROM blocked_events_ephemeral WHERE recorded_by = ?",
    (laptop_peer_id,)
)
print(f"\nBlocked events for laptop ({len(blocked)}):")
for b in blocked:
    print(f"  recorded_id={b['recorded_id'][:20]}, deps_remaining={b['deps_remaining']}")

# Check blocked deps
blocked_deps = db.query_all(
    "SELECT recorded_id, dep_id FROM blocked_event_deps_ephemeral WHERE recorded_by = ?",
    (laptop_peer_id,)
)
print(f"\nBlocked deps for laptop ({len(blocked_deps)}):")
for d in blocked_deps:
    print(f"  recorded_id={d['recorded_id'][:20]}, waiting for dep_id={d['dep_id'][:20]}")

# Do 60 sync ticks
print(f"\n=== Running 60 sync ticks ===")
for i in range(60):
    tick.tick(t_ms=4000 + i*100, db=db)
db.commit()

# Check users table for laptop AFTER sync
laptop_users_after = db.query_all(
    "SELECT user_id, peer_id, name FROM users WHERE recorded_by = ?",
    (laptop_peer_id,)
)
print(f"\nUsers visible to laptop AFTER sync ({len(laptop_users_after)}):")
for u in laptop_users_after:
    print(f"  user_id={u['user_id'][:20]}, peer_id={u['peer_id'][:20] if u['peer_id'] else 'None'}, name={u['name']}")

# Debug: show ALL users in the database
all_users = db.query_all("SELECT user_id, peer_id, name, recorded_by FROM users")
print(f"\nAll users in database ({len(all_users)}):")
for u in all_users:
    print(f"  user_id={u['user_id'][:20]}, peer_id={u['peer_id'][:20] if u['peer_id'] else 'None'}, recorded_by={u['recorded_by'][:20]}")

# Check linked_peers table for laptop
linked_peers = db.query_all(
    "SELECT link_id, user_id, peer_id FROM linked_peers WHERE recorded_by = ?",
    (laptop_peer_id,)
)
print(f"\nLinked peers visible to laptop ({len(linked_peers)}):")
for lp in linked_peers:
    print(f"  link_id={lp['link_id'][:20]}, user_id={lp['user_id'][:20]}, peer_id={lp['peer_id'][:20]}")

# Check if the link event is valid
link_id = alice_laptop['link_id']
link_valid = db.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (link_id, laptop_peer_id)
)
print(f"\nLink event valid for laptop: {bool(link_valid)}")
print(f"  link_id: {link_id}")

# Check if link event is blocked
link_blocked = db.query_one(
    "SELECT recorded_id, deps_remaining FROM blocked_events_ephemeral WHERE recorded_by = ? AND recorded_id IN (SELECT recorded_id FROM recorded_events WHERE ref_id = ? AND recorded_by = ?)",
    (laptop_peer_id, link_id, laptop_peer_id)
)
if link_blocked:
    print(f"  link is BLOCKED: recorded_id={link_blocked['recorded_id'][:20]}, deps_remaining={link_blocked['deps_remaining']}")
    # Get what deps are missing
    link_deps = db.query_all(
        "SELECT dep_id FROM blocked_event_deps_ephemeral WHERE recorded_id = ? AND recorded_by = ?",
        (link_blocked['recorded_id'], laptop_peer_id)
    )
    for d in link_deps:
        dep_valid = db.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (d['dep_id'], laptop_peer_id)
        )
        print(f"    waiting on: {d['dep_id']}, valid: {bool(dep_valid)}")

# Check blocked events after sync
blocked_after = db.query_all(
    "SELECT recorded_id, deps_remaining FROM blocked_events_ephemeral WHERE recorded_by = ?",
    (laptop_peer_id,)
)
print(f"\nBlocked events for laptop AFTER sync ({len(blocked_after)}):")
for b in blocked_after:
    print(f"  recorded_id={b['recorded_id'][:20]}, deps_remaining={b['deps_remaining']}")

# Check all valid_events count
valid_count = db.query_one(
    "SELECT COUNT(*) as cnt FROM valid_events WHERE recorded_by = ?",
    (laptop_peer_id,)
)
print(f"\nTotal valid events for laptop: {valid_count['cnt']}")

# Check what's blocking the remaining event
blocked_deps_after = db.query_all(
    "SELECT recorded_id, dep_id FROM blocked_event_deps_ephemeral WHERE recorded_by = ?",
    (laptop_peer_id,)
)
print(f"\nBlocked deps for laptop AFTER sync ({len(blocked_deps_after)}):")
for d in blocked_deps_after:
    print(f"  recorded_id={d['recorded_id'][:20]}, waiting for dep_id={d['dep_id']}")
    # Check if this dep is valid
    dep_valid = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (d['dep_id'], laptop_peer_id)
    )
    print(f"    dep is valid: {bool(dep_valid)}")
    # Try to get the blob to see what type it is
    import store
    dep_blob = store.get(d['dep_id'], db)
    if dep_blob:
        import crypto
        dep_data = crypto.parse_json(dep_blob)
        print(f"    dep event type: {dep_data.get('type')}")
        print(f"    dep event fields: {list(dep_data.keys())}")
    else:
        print(f"    dep blob NOT in store!")
    # Also check the blocked recorded event
    rec_blob = store.get(d['recorded_id'], db)
    if rec_blob:
        import crypto
        rec_data = crypto.parse_json(rec_blob)
        ref_id = rec_data.get('ref_id')
        print(f"    blocked event ref_id: {ref_id}")
        ref_blob = store.get(ref_id, db)
        if ref_blob:
            ref_data = crypto.parse_json(ref_blob)
            print(f"    blocked event type: {ref_data.get('type')}")

# Check if network is valid
network_id = alice_phone['network_id']
network_valid = db.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (network_id, laptop_peer_id)
)
print(f"\nNetwork valid for laptop: {bool(network_valid)}")

# Check if user event is valid
user_id = alice_phone['user_id']
user_valid = db.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (user_id, laptop_peer_id)
)
print(f"User valid for laptop: {bool(user_valid)}")
