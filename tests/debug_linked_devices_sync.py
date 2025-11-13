"""Debug script to check why linked devices don't see each other's messages."""
import sqlite3
from db import Database, create_unsafe_db, create_safe_db
import schema
from events.identity import user, link_invite, link
from events.content import message
import tick

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

print("\n=== Setup: Alice creates network on phone ===")
alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Phone peer_id: {alice_phone['peer_id'][:20]}...")
print(f"Phone peer_shared_id: {alice_phone['peer_shared_id'][:20]}...")
print(f"Phone user_id: {alice_phone['user_id'][:20]}...")

db.commit()

print("\n=== Alice creates link invite ===")
link_invite_id, link_url, link_data = link_invite.create(
    peer_id=alice_phone['peer_id'],
    t_ms=2000,
    db=db
)
print(f"Link invite ID: {link_invite_id[:20]}...")

db.commit()

print("\n=== Alice links laptop ===")
alice_laptop = link.join(link_url=link_url, t_ms=3000, db=db)
print(f"Laptop peer_id: {alice_laptop['peer_id'][:20]}...")
print(f"Laptop peer_shared_id: {alice_laptop['peer_shared_id'][:20]}...")
print(f"Laptop user_id: {alice_laptop['user_id'][:20]}...")

db.commit()

print(f"\n✓ Same user_id: {alice_laptop['user_id'] == alice_phone['user_id']}")
print(f"✓ Different peer_id: {alice_laptop['peer_id'] != alice_phone['peer_id']}")

# Sync multiple rounds
print("\n=== Running 20 sync rounds ===")
for i in range(20):
    tick.tick(t_ms=4000 + i*100, db=db)

# Check sync connections
unsafedb = create_unsafe_db(db)
connections = unsafedb.query("SELECT peer_shared_id, last_seen_ms FROM sync_connections")
print(f"\nSync connections: {len(connections)}")
for conn in connections:
    peer_shared_id = conn['peer_shared_id']
    print(f"  - {peer_shared_id[:20]}...")
    if peer_shared_id == alice_phone['peer_shared_id']:
        print(f"    (PHONE)")
    if peer_shared_id == alice_laptop['peer_shared_id']:
        print(f"    (LAPTOP)")

# Check if each device knows about the other's peer_shared
phone_safedb = create_safe_db(db, recorded_by=alice_phone['peer_id'])
laptop_safedb = create_safe_db(db, recorded_by=alice_laptop['peer_id'])

phone_knows_laptop = phone_safedb.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (alice_laptop['peer_shared_id'], alice_phone['peer_id'])
)
laptop_knows_phone = laptop_safedb.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (alice_phone['peer_shared_id'], alice_laptop['peer_id'])
)

print(f"\nPhone knows about laptop's peer_shared: {bool(phone_knows_laptop)}")
print(f"Laptop knows about phone's peer_shared: {bool(laptop_knows_phone)}")

# Check peers_shared table
phone_peers = phone_safedb.query(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (alice_phone['peer_id'],)
)
laptop_peers = laptop_safedb.query(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (alice_laptop['peer_id'],)
)

print(f"\nPhone sees {len(phone_peers)} peers in peers_shared")
print(f"Laptop sees {len(laptop_peers)} peers in peers_shared")

# Check invite_accepteds table
phone_invite_accepteds = phone_safedb.query(
    "SELECT inviter_peer_shared_id FROM invite_accepteds WHERE recorded_by = ?",
    (alice_phone['peer_id'],)
)
laptop_invite_accepteds = laptop_safedb.query(
    "SELECT inviter_peer_shared_id FROM invite_accepteds WHERE recorded_by = ?",
    (alice_laptop['peer_id'],)
)

print(f"\nPhone has {len(phone_invite_accepteds)} invite_accepteds entries")
if phone_invite_accepteds:
    for row in phone_invite_accepteds:
        inviter = row['inviter_peer_shared_id']
        print(f"  Inviter: {inviter[:20]}...")
        if inviter == alice_laptop['peer_shared_id']:
            print(f"    (LAPTOP)")

print(f"Laptop has {len(laptop_invite_accepteds)} invite_accepteds entries")
if laptop_invite_accepteds:
    for row in laptop_invite_accepteds:
        inviter = row['inviter_peer_shared_id']
        print(f"  Inviter: {inviter[:20]}...")
        if inviter == alice_phone['peer_shared_id']:
            print(f"    (PHONE)")

# Create messages
print("\n=== Creating messages ===")
phone_msg = message.create(
    peer_id=alice_phone['peer_id'],
    channel_id=alice_phone['channel_id'],
    content="From phone",
    t_ms=5000,
    db=db
)
db.commit()
print(f"Phone message: {phone_msg['id'][:20]}...")

laptop_msg = message.create(
    peer_id=alice_laptop['peer_id'],
    channel_id=alice_phone['channel_id'],  # Same channel
    content="From laptop",
    t_ms=5100,
    db=db
)
db.commit()
print(f"Laptop message: {laptop_msg['id'][:20]}...")

# Check recorded events
phone_recorded = unsafedb.query(
    "SELECT event_id FROM recorded WHERE recorded_by = ? LIMIT 5",
    (alice_phone['peer_id'],)
)
laptop_recorded = unsafedb.query(
    "SELECT event_id FROM recorded WHERE recorded_by = ? LIMIT 5",
    (alice_laptop['peer_id'],)
)

print(f"\nPhone has {len(phone_recorded)} recorded events (showing first 5)")
print(f"Laptop has {len(laptop_recorded)} recorded events (showing first 5)")

# Check peers_shared BEFORE message sync
print("\n=== Before message sync ===")
phone_peers_before = phone_safedb.query(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (alice_phone['peer_id'],)
)
laptop_peers_before = laptop_safedb.query(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (alice_laptop['peer_id'],)
)
print(f"Phone sees {len(phone_peers_before)} peers")
print(f"Laptop sees {len(laptop_peers_before)} peers")

# Sync messages
print("\n=== Syncing messages (10 rounds) ===")
for i in range(10):
    tick.tick(t_ms=6000 + i*100, db=db)

# Check peers_shared AFTER message sync
print("\n=== After message sync ===")
phone_peers_after = phone_safedb.query(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (alice_phone['peer_id'],)
)
laptop_peers_after = laptop_safedb.query(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (alice_laptop['peer_id'],)
)
print(f"Phone sees {len(phone_peers_after)} peers")
for row in phone_peers_after:
    peer_id = row['peer_shared_id']
    print(f"  - {peer_id[:20]}...")
    if peer_id == alice_laptop['peer_shared_id']:
        print(f"    (LAPTOP!)")

print(f"Laptop sees {len(laptop_peers_after)} peers")
for row in laptop_peers_after:
    peer_id = row['peer_shared_id']
    print(f"  - {peer_id[:20]}...")
    if peer_id == alice_phone['peer_shared_id']:
        print(f"    (PHONE!)")

# Check messages received
phone_messages = message.list_messages(alice_phone['channel_id'], alice_phone['peer_id'], db)
laptop_messages = message.list_messages(alice_phone['channel_id'], alice_laptop['peer_id'], db)

phone_contents = [msg['content'] for msg in phone_messages]
laptop_contents = [msg['content'] for msg in laptop_messages]

print(f"\n=== Final Results ===")
print(f"Phone sees {len(phone_messages)} messages: {phone_contents}")
print(f"Laptop sees {len(laptop_messages)} messages: {laptop_contents}")

# Check if devices are in same network/groups
phone_groups = phone_safedb.query(
    "SELECT group_id FROM group_members WHERE user_id = ?",
    (alice_phone['user_id'],)
)
laptop_groups = laptop_safedb.query(
    "SELECT group_id FROM group_members WHERE user_id = ?",
    (alice_laptop['user_id'],)
)

print(f"\nPhone is in {len(phone_groups)} groups")
print(f"Laptop is in {len(laptop_groups)} groups")
