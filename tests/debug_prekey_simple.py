"""Debug: Why can't Device 1 unwrap Device 2's sync_connect?"""
import sqlite3
import base64
import json
from db import Database, create_unsafe_db, create_safe_db
import schema
from events.identity import user, link_invite, link
from events.group import group, group_member
from events.transit import transit_prekey
import tick
import crypto

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

print("\n=== Test scenario matching test_link_device_new_groups ===")

# Device 1 creates network
alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Device 1 peer_id: {alice_device1['peer_id'][:20]}...")
print(f"Device 1 peer_shared_id: {alice_device1['peer_shared_id'][:20]}...")

# Get Device 1's initial prekey_id
dev1_initial_prekey_rows = db.query(
    "SELECT prekey_id FROM transit_prekeys WHERE owner_peer_id = ?",
    (alice_device1['peer_id'],)
)
dev1_initial_prekey_id = dev1_initial_prekey_rows[0]['prekey_id'] if dev1_initial_prekey_rows else None
print(f"Device 1's initial prekey_id: {crypto.b64encode(dev1_initial_prekey_id) if dev1_initial_prekey_id else 'NONE'}")

# Create Group A
group_a_id, group_a_key_id = group.create(
    name='Group A',
    peer_id=alice_device1['peer_id'],
    peer_shared_id=alice_device1['peer_shared_id'],
    t_ms=2000,
    db=db
)
group_member.create(
    group_id=group_a_id,
    user_id=alice_device1['user_id'],
    peer_id=alice_device1['peer_id'],
    peer_shared_id=alice_device1['peer_shared_id'],
    t_ms=2001,
    db=db
)
db.commit()

# Sync 5 rounds
print("\n=== Sync 5 rounds ===")
for i in range(5):
    tick.tick(t_ms=2500 + i*100, db=db)

# Create link invite
print("\n=== Device 1 creates link invite ===")
invite_id, invite_link, invite_data = link_invite.create(
    peer_id=alice_device1['peer_id'],
    t_ms=3000,
    db=db
)

# Parse link URL to see what prekey_id is embedded
link_code = invite_link.replace('quiet://link/', '')
padding = (4 - len(link_code) % 4) % 4
link_code_padded = link_code + ('=' * padding)
link_json = base64.urlsafe_b64decode(link_code_padded).decode()
link_data_parsed = json.loads(link_json)

invite_blob_b64 = link_data_parsed['invite_blob']
invite_blob = base64.urlsafe_b64decode(invite_blob_b64 + '===')
invite_event = crypto.parse_json(invite_blob)
invite_prekey_id_b64 = invite_event.get('inviter_transit_prekey_id', 'MISSING')
invite_prekey_id = crypto.b64decode(invite_prekey_id_b64) if invite_prekey_id_b64 != 'MISSING' else None

print(f"Link URL contains prekey_id: {crypto.b64encode(invite_prekey_id) if invite_prekey_id else 'NONE'}")
print(f"Does it match Device 1's initial prekey? {invite_prekey_id == dev1_initial_prekey_id}")

db.commit()

# Create Group B + sync
group_b_id, group_b_key_id = group.create(
    name='Group B',
    peer_id=alice_device1['peer_id'],
    peer_shared_id=alice_device1['peer_shared_id'],
    t_ms=3500,
    db=db
)
group_member.create(
    group_id=group_b_id,
    user_id=alice_device1['user_id'],
    peer_id=alice_device1['peer_id'],
    peer_shared_id=alice_device1['peer_shared_id'],
    t_ms=3501,
    db=db
)
db.commit()

print("\n=== Sync 5 more rounds ===")
for i in range(5):
    tick.tick(t_ms=4000 + i*100, db=db)

# Now Device 2 joins
print("\n=== Device 2 joins ===")
alice_device2 = link.join(link_url=invite_link, t_ms=5000, db=db)
print(f"Device 2 peer_id: {alice_device2['peer_id'][:20]}...")
print(f"Device 2 peer_shared_id: {alice_device2['peer_shared_id'][:20]}...")
db.commit()

# What prekey will Device 2 use to wrap sync_connect?
dev1_prekey_for_device2 = transit_prekey.get_transit_prekey_for_peer(
    alice_device1['peer_shared_id'],
    alice_device2['peer_id'],
    db
)

if dev1_prekey_for_device2:
    dev1_prekey_id_device2_will_use = dev1_prekey_for_device2['id']
    print(f"\n=== Device 2 will wrap with prekey_id: {crypto.b64encode(dev1_prekey_id_device2_will_use)} ===")
    print(f"Does this match Device 1's initial prekey? {dev1_prekey_id_device2_will_use == dev1_initial_prekey_id}")

    # Check if Device 1 still has this prekey
    dev1_has_prekey = db.query_one(
        "SELECT 1 FROM transit_prekeys WHERE prekey_id = ? AND owner_peer_id = ?",
        (dev1_prekey_id_device2_will_use, alice_device1['peer_id'])
    )
    if dev1_has_prekey:
        print(f"  ✓ Device 1 STILL HAS this prekey in transit_prekeys")
    else:
        print(f"  ✗ Device 1 does NOT have this prekey!")
else:
    print(f"\n✗ Device 2 cannot get Device 1's prekey!")
