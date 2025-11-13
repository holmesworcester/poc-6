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

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

print("\n=== Setup: Device 1 creates network and Group A ===")
alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Device 1 peer_id: {alice_device1['peer_id'][:20]}...")
print(f"Device 1 peer_shared_id: {alice_device1['peer_shared_id'][:20]}...")
db.commit()

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

# Check Device 1's transit_prekeys BEFORE sync
unsafedb = create_unsafe_db(db)
safedb1 = create_safe_db(db, recorded_by=alice_device1['peer_id'])
dev1_prekeys_before_rows = db.query(
    "SELECT prekey_id FROM transit_prekeys WHERE owner_peer_id = ?",
    (alice_device1['peer_id'],)
)
dev1_prekeys_before = [row['prekey_id'] for row in dev1_prekeys_before_rows]
print(f"\n=== Device 1's transit_prekeys BEFORE sync: {len(dev1_prekeys_before)} ===")
for prekey_id in dev1_prekeys_before:
    import crypto
    print(f"  prekey_id={crypto.b64encode(prekey_id)[:20]}...")

# Sync 5 rounds (like test_link_device_new_groups)
print("\n=== Sync 5 rounds ===")
for i in range(5):
    tick.tick(t_ms=2500 + i*100, db=db)

# Check Device 1's transit_prekeys AFTER sync
dev1_prekeys_after = safedb1.query(
    "SELECT prekey_id FROM transit_prekeys WHERE owner_peer_id = ? AND recorded_by = ?",
    (alice_device1['peer_id'], alice_device1['peer_id'])
)
print(f"\n=== Device 1's transit_prekeys AFTER 5 syncs: {len(dev1_prekeys_after)} ===")
for row in dev1_prekeys_after:
    print(f"  prekey_id={row['prekey_id'][:20]}...")

# Create link invite
print("\n=== Device 1 creates link invite ===")
invite_id, invite_link, invite_data = link_invite.create(
    peer_id=alice_device1['peer_id'],
    t_ms=3000,
    db=db
)
print(f"Link invite created: {invite_id[:20]}...")

# Parse link URL to see what prekey_id is in it
link_code = invite_link.replace('quiet://link/', '')
padding = (4 - len(link_code) % 4) % 4
link_code_padded = link_code + ('=' * padding)
link_json = base64.urlsafe_b64decode(link_code_padded).decode()
link_data_parsed = json.loads(link_json)

print(f"\n=== Link URL contains prekey info: ===")
if 'invite_blob' in link_data_parsed:
    invite_blob_b64 = link_data_parsed['invite_blob']
    invite_blob = base64.urlsafe_b64decode(invite_blob_b64 + '===')
    import crypto
    invite_event = crypto.parse_json(invite_blob)
    invite_prekey_id = invite_event.get('inviter_transit_prekey_id', 'MISSING')
    print(f"  inviter_transit_prekey_id in invite: {invite_prekey_id[:20] if invite_prekey_id != 'MISSING' else 'MISSING'}...")

db.commit()

# Create Group B
print("\n=== Device 1 creates Group B ===")
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

# Sync 5 more rounds
print("\n=== Sync 5 more rounds ===")
for i in range(5):
    tick.tick(t_ms=4000 + i*100, db=db)

# Check Device 1's transit_prekeys after more syncs
dev1_prekeys_after2 = safedb1.query(
    "SELECT prekey_id FROM transit_prekeys WHERE owner_peer_id = ? AND recorded_by = ?",
    (alice_device1['peer_id'], alice_device1['peer_id'])
)
print(f"\n=== Device 1's transit_prekeys AFTER 10 total syncs: {len(dev1_prekeys_after2)} ===")
for row in dev1_prekeys_after2:
    print(f"  prekey_id={row['prekey_id'][:20]}...")

# Now Device 2 joins
print("\n=== Device 2 joins ===")
alice_device2 = link.join(link_url=invite_link, t_ms=5000, db=db)
print(f"Device 2 peer_id: {alice_device2['peer_id'][:20]}...")
print(f"Device 2 peer_shared_id: {alice_device2['peer_shared_id'][:20]}...")
db.commit()

# Check what prekey Device 2 sees for Device 1
safedb2 = create_safe_db(db, recorded_by=alice_device2['peer_id'])
dev1_prekeys_device2_sees = safedb2.query(
    "SELECT transit_prekey_id, transit_prekey_shared_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (alice_device1['peer_shared_id'], alice_device2['peer_id'])
)
print(f"\n=== Device 2's view of Device 1's transit_prekeys: {len(dev1_prekeys_device2_sees)} ===")
for row in dev1_prekeys_device2_sees:
    print(f"  prekey_id={row['transit_prekey_id'][:20] if row['transit_prekey_id'] else 'NULL'}...")

# Try to get Device 1's prekey as Device 2 would
dev1_prekey_for_device2 = transit_prekey.get_transit_prekey_for_peer(
    alice_device1['peer_shared_id'],
    alice_device2['peer_id'],
    db
)
if dev1_prekey_for_device2:
    dev1_prekey_id_used = dev1_prekey_for_device2['id']
    import crypto
    print(f"\n=== Device 2 will wrap sync_connect with prekey_id: {crypto.b64encode(dev1_prekey_id_used)[:20]}... ===")

    # Check if Device 1 has this prekey_id in its transit_prekeys
    dev1_has_this_key = safedb1.query_one(
        "SELECT 1 FROM transit_prekeys WHERE prekey_id = ? AND owner_peer_id = ? AND recorded_by = ?",
        (dev1_prekey_id_used, alice_device1['peer_id'], alice_device1['peer_id'])
    )
    if dev1_has_this_key:
        print(f"  ✓ Device 1 HAS this prekey in its transit_prekeys table")
    else:
        print(f"  ✗ Device 1 does NOT have this prekey in its transit_prekeys table!")
        print(f"     This is why Device 1 cannot unwrap Device 2's sync_connect!")
else:
    print(f"\n=== Device 2 cannot get Device 1's prekey! ===")
