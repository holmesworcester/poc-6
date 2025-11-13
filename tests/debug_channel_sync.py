"""Debug: Why doesn't laptop have the channel after syncing?"""
import sqlite3
from db import Database, create_unsafe_db, create_safe_db
import schema
from events.identity import user, link_invite, link
import tick

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

print("\n=== Setup ===")
alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Phone user_id: {alice_phone['user_id'][:20]}...")
print(f"Phone channel_id: {alice_phone['channel_id'][:20]}...")

db.commit()

link_invite_id, link_url, link_data = link_invite.create(
    peer_id=alice_phone['peer_id'],
    t_ms=2000,
    db=db
)

db.commit()

alice_laptop = link.join(link_url=link_url, t_ms=3000, db=db)
print(f"Laptop user_id: {alice_laptop['user_id'][:20]}...")
print(f"Same user_id: {alice_phone['user_id'] == alice_laptop['user_id']}")

db.commit()

# Sync
print("\n=== Syncing (20 rounds) ===")
for i in range(20):
    tick.tick(t_ms=4000 + i*100, db=db)

# Check channels
print("\n=== Checking channels ===")

phone_safedb = create_safe_db(db, recorded_by=alice_phone['peer_id'])
laptop_safedb = create_safe_db(db, recorded_by=alice_laptop['peer_id'])

phone_channels = phone_safedb.query(
    "SELECT channel_id, name, is_main FROM channels WHERE recorded_by = ?",
    (alice_phone['peer_id'],)
)
laptop_channels = laptop_safedb.query(
    "SELECT channel_id, name, is_main FROM channels WHERE recorded_by = ?",
    (alice_laptop['peer_id'],)
)

print(f"\nPhone sees {len(phone_channels)} channels:")
for row in phone_channels:
    print(f"  {row['name']}: {row['channel_id'][:20]}... (is_main={row['is_main']})")

print(f"\nLaptop sees {len(laptop_channels)} channels:")
for row in laptop_channels:
    print(f"  {row['name']}: {row['channel_id'][:20]}... (is_main={row['is_main']})")

if len(laptop_channels) == 0:
    print("\n✗ PROBLEM: Laptop has NO channels!")
else:
    laptop_has_phone_channel = any(row['channel_id'] == alice_phone['channel_id'] for row in laptop_channels)
    if laptop_has_phone_channel:
        print(f"\n✓ Laptop has phone's channel")
    else:
        print(f"\n✗ Laptop has channels but NOT phone's channel")

# Check if laptop knows about the channel event
unsafedb = create_unsafe_db(db)
laptop_valid_channel = laptop_safedb.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (alice_phone['channel_id'], alice_laptop['peer_id'])
)

if laptop_valid_channel:
    print(f"✓ Laptop has channel event in valid_events")
else:
    print(f"✗ Laptop does NOT have channel event in valid_events")

# Check if channel was synced to laptop (in recorded table)
laptop_recorded_channel = unsafedb.query_one(
    "SELECT recorded_at FROM recorded WHERE event_id = ? AND recorded_by = ?",
    (alice_phone['channel_id'], alice_laptop['peer_id'])
)

if laptop_recorded_channel:
    print(f"✓ Channel was synced to laptop (recorded_at={laptop_recorded_channel['recorded_at']})")
else:
    print(f"✗ Channel was NOT synced to laptop")

# Check users table
print("\n=== Checking users table ===")

phone_users = phone_safedb.query(
    "SELECT user_id, peer_id FROM users WHERE recorded_by = ?",
    (alice_phone['peer_id'],)
)
laptop_users = laptop_safedb.query(
    "SELECT user_id, peer_id FROM users WHERE recorded_by = ?",
    (alice_laptop['peer_id'],)
)

print(f"\nPhone sees {len(phone_users)} users:")
for row in phone_users:
    print(f"  user_id={row['user_id'][:20]}... peer_id={row['peer_id'][:20]}...")
    if row['peer_id'] == alice_phone['peer_shared_id']:
        print(f"    ^^ Phone's peer")
    if row['peer_id'] == alice_laptop['peer_shared_id']:
        print(f"    ^^ Laptop's peer")

print(f"\nLaptop sees {len(laptop_users)} users:")
for row in laptop_users:
    print(f"  user_id={row['user_id'][:20]}... peer_id={row['peer_id'][:20]}...")
    if row['peer_id'] == alice_phone['peer_shared_id']:
        print(f"    ^^ Phone's peer")
    if row['peer_id'] == alice_laptop['peer_shared_id']:
        print(f"    ^^ Laptop's peer")

# Check if laptop has phone's user event
laptop_has_phone_user = any(row['peer_id'] == alice_phone['peer_shared_id'] for row in laptop_users)
if laptop_has_phone_user:
    print(f"\n✓ Laptop sees phone's user/peer")
else:
    print(f"\n✗ Laptop does NOT see phone's user/peer")

phone_has_laptop_user = any(row['peer_id'] == alice_laptop['peer_shared_id'] for row in phone_users)
if phone_has_laptop_user:
    print(f"✓ Phone sees laptop's user/peer")
else:
    print(f"✗ Phone does NOT see laptop's user/peer")
