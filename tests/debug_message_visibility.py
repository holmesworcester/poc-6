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
print(f"Phone channel_id: {alice_phone['channel_id'][:20]}...")
print(f"Phone key_id: {alice_phone['key_id'][:20]}...")

db.commit()

print("\n=== Alice creates link invite ===")
link_invite_id, link_url, link_data = link_invite.create(
    peer_id=alice_phone['peer_id'],
    t_ms=2000,
    db=db
)
print(f"Link invite created")

db.commit()

print("\n=== Alice links laptop ===")
alice_laptop = link.join(link_url=link_url, t_ms=3000, db=db)
print(f"Laptop peer_id: {alice_laptop['peer_id'][:20]}...")
print(f"Laptop peer_shared_id: {alice_laptop['peer_shared_id'][:20]}...")
print(f"Laptop user_id: {alice_laptop['user_id'][:20]}...")

db.commit()

# Sync
print("\n=== Syncing (20 rounds) ===")
for i in range(20):
    tick.tick(t_ms=4000 + i*100, db=db)

# Create messages
print("\n=== Creating messages ===")
phone_msg = message.create(
    peer_id=alice_phone['peer_id'],
    channel_id=alice_phone['channel_id'],
    content="From phone",
    t_ms=6000,
    db=db
)
print(f"Phone message: {phone_msg['id'][:20]}...")

# Get laptop's view of the channel
laptop_safedb_temp = create_safe_db(db, recorded_by=alice_laptop['peer_id'])
laptop_channel_row = laptop_safedb_temp.query_one(
    "SELECT channel_id FROM channels WHERE recorded_by = ? AND is_main = 1 LIMIT 1",
    (alice_laptop['peer_id'],)
)

if laptop_channel_row:
    laptop_channel_id = laptop_channel_row['channel_id']
    print(f"Laptop sees channel: {laptop_channel_id[:20]}...")

    laptop_msg = message.create(
        peer_id=alice_laptop['peer_id'],
        channel_id=laptop_channel_id,  # Laptop's view of channel
        content="From laptop",
        t_ms=6100,
        db=db
    )
    print(f"Laptop message: {laptop_msg['id'][:20]}...")
else:
    print("ERROR: Laptop doesn't have any channels!")
    laptop_msg = None

db.commit()

# Sync messages
print("\n=== Syncing messages (10 rounds) ===")
for i in range(10):
    tick.tick(t_ms=7000 + i*100, db=db)

# === INVESTIGATION 1: Check encryption keys ===
print("\n=== INVESTIGATION 1: Encryption Keys ===")

phone_safedb = create_safe_db(db, recorded_by=alice_phone['peer_id'])
laptop_safedb = create_safe_db(db, recorded_by=alice_laptop['peer_id'])

# Check what keys each device has
phone_keys = phone_safedb.query(
    "SELECT key_id, group_id FROM group_keys WHERE recorded_by = ?",
    (alice_phone['peer_id'],)
)
laptop_keys = laptop_safedb.query(
    "SELECT key_id, group_id FROM group_keys WHERE recorded_by = ?",
    (alice_laptop['peer_id'],)
)

print(f"\nPhone has {len(phone_keys)} group keys:")
for row in phone_keys:
    print(f"  key_id={row['key_id'][:20]}... group_id={row['group_id'][:20]}...")
    if row['key_id'] == alice_phone['key_id']:
        print(f"    (PHONE'S ORIGINAL KEY)")

print(f"\nLaptop has {len(laptop_keys)} group keys:")
for row in laptop_keys:
    print(f"  key_id={row['key_id'][:20]}... group_id={row['group_id'][:20]}...")
    if row['key_id'] == alice_phone['key_id']:
        print(f"    (PHONE'S KEY - SYNCED!)")

# Check if messages are encrypted with different keys
unsafedb = create_unsafe_db(db)
import store, crypto

phone_msg_blob = store.get(phone_msg['id'], unsafedb)
phone_msg_data = crypto.parse_json(phone_msg_blob)
print(f"\nPhone message key_id: {phone_msg_data.get('key_id', 'NONE')[:20]}...")

laptop_msg_blob = store.get(laptop_msg['id'], unsafedb)
laptop_msg_data = crypto.parse_json(laptop_msg_blob)
print(f"Laptop message key_id: {laptop_msg_data.get('key_id', 'NONE')[:20]}...")

if phone_msg_data.get('key_id') == laptop_msg_data.get('key_id'):
    print("✓ Both messages use the SAME key_id")
else:
    print("✗ Messages use DIFFERENT key_ids!")

# === INVESTIGATION 2: Check channels ===
print("\n=== INVESTIGATION 2: Channels ===")

phone_channels = phone_safedb.query(
    "SELECT channel_id, name FROM channels WHERE recorded_by = ?",
    (alice_phone['peer_id'],)
)
laptop_channels = laptop_safedb.query(
    "SELECT channel_id, name FROM channels WHERE recorded_by = ?",
    (alice_laptop['peer_id'],)
)

print(f"\nPhone sees {len(phone_channels)} channels:")
for row in phone_channels:
    print(f"  channel_id={row['channel_id'][:20]}... name={row['name']}")
    if row['channel_id'] == alice_phone['channel_id']:
        print(f"    (PHONE'S CHANNEL)")

print(f"\nLaptop sees {len(laptop_channels)} channels:")
for row in laptop_channels:
    print(f"  channel_id={row['channel_id'][:20]}... name={row['name']}")
    if row['channel_id'] == alice_phone['channel_id']:
        print(f"    (PHONE'S CHANNEL - SYNCED!)")

# === INVESTIGATION 3: Check user identity ===
print("\n=== INVESTIGATION 3: User Identity ===")

# Check if both devices see themselves as the same user
phone_user_rows = phone_safedb.query(
    "SELECT user_id, peer_id FROM users WHERE recorded_by = ?",
    (alice_phone['peer_id'],)
)
laptop_user_rows = laptop_safedb.query(
    "SELECT user_id, peer_id FROM users WHERE recorded_by = ?",
    (alice_laptop['peer_id'],)
)

print(f"\nPhone sees {len(phone_user_rows)} users:")
for row in phone_user_rows:
    print(f"  user_id={row['user_id'][:20]}... peer_id={row['peer_id'][:20]}...")
    if row['user_id'] == alice_phone['user_id']:
        print(f"    (Alice's user)")
    if row['peer_id'] == alice_phone['peer_shared_id']:
        print(f"    (Phone's peer)")
    if row['peer_id'] == alice_laptop['peer_shared_id']:
        print(f"    (Laptop's peer)")

print(f"\nLaptop sees {len(laptop_user_rows)} users:")
for row in laptop_user_rows:
    print(f"  user_id={row['user_id'][:20]}... peer_id={row['peer_id'][:20]}...")
    if row['user_id'] == alice_laptop['user_id']:
        print(f"    (Alice's user)")
    if row['peer_id'] == alice_phone['peer_shared_id']:
        print(f"    (Phone's peer)")
    if row['peer_id'] == alice_laptop['peer_shared_id']:
        print(f"    (Laptop's peer)")

# Check if both devices map to the same user_id
if alice_phone['user_id'] == alice_laptop['user_id']:
    print(f"\n✓ Both devices have the SAME user_id: {alice_phone['user_id'][:20]}...")
else:
    print(f"\n✗ Devices have DIFFERENT user_ids!")
    print(f"  Phone: {alice_phone['user_id'][:20]}...")
    print(f"  Laptop: {alice_laptop['user_id'][:20]}...")

# Check how messages table looks
print("\n=== INVESTIGATION 4: Messages Table ===")

phone_messages_raw = phone_safedb.query(
    """SELECT message_id, sender_user_id, channel_id, content
       FROM messages WHERE recorded_by = ?""",
    (alice_phone['peer_id'],)
)
laptop_messages_raw = laptop_safedb.query(
    """SELECT message_id, sender_user_id, channel_id, content
       FROM messages WHERE recorded_by = ?""",
    (alice_laptop['peer_id'],)
)

print(f"\nPhone's messages table ({len(phone_messages_raw)} rows):")
for row in phone_messages_raw:
    print(f"  message_id={row['message_id'][:20]}...")
    print(f"    sender_user_id={row['sender_user_id'][:20]}...")
    print(f"    channel_id={row['channel_id'][:20]}...")
    print(f"    content={row['content']}")
    if row['sender_user_id'] == alice_phone['user_id']:
        print(f"    (sender is Alice)")
    if row['channel_id'] == alice_phone['channel_id']:
        print(f"    (in phone's channel)")

print(f"\nLaptop's messages table ({len(laptop_messages_raw)} rows):")
for row in laptop_messages_raw:
    print(f"  message_id={row['message_id'][:20]}...")
    print(f"    sender_user_id={row['sender_user_id'][:20]}...")
    print(f"    channel_id={row['channel_id'][:20]}...")
    print(f"    content={row['content']}")
    if row['sender_user_id'] == alice_laptop['user_id']:
        print(f"    (sender is Alice)")
    if row['channel_id'] == alice_phone['channel_id']:
        print(f"    (in phone's channel)")

# Check what list_messages returns
print("\n=== INVESTIGATION 5: list_messages() Results ===")
phone_messages = message.list_messages(alice_phone['channel_id'], alice_phone['peer_id'], db)

if laptop_msg:
    laptop_messages = message.list_messages(laptop_channel_id, alice_laptop['peer_id'], db)
else:
    laptop_messages = []

print(f"\nPhone's list_messages() returns {len(phone_messages)} messages:")
for msg in phone_messages:
    print(f"  {msg['content']}")

print(f"\nLaptop's list_messages() returns {len(laptop_messages)} messages:")
for msg in laptop_messages:
    print(f"  {msg['content']}")
