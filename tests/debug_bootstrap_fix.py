"""Debug: Does the bootstrap fix in link.join() actually work?"""
import sqlite3
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
db.commit()

# Create Group B + sync (like test_link_device_new_groups)
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

# Check Device 1's transit_prekeys_shared BEFORE Device 2 joins
print("\n=== Device 1's transit_prekeys_shared BEFORE Device 2 joins ===")
safedb1 = create_safe_db(db, recorded_by=alice_device1['peer_id'])
dev1_prekeys_before = safedb1.query(
    "SELECT peer_id, transit_prekey_id FROM transit_prekeys_shared WHERE recorded_by = ?",
    (alice_device1['peer_id'],)
)
print(f"Device 1 has {len(dev1_prekeys_before)} transit_prekeys_shared:")
for row in dev1_prekeys_before:
    peer_id_short = row['peer_id'][:10] + '...' if row['peer_id'] else 'NULL'
    is_self = row['peer_id'] == alice_device1['peer_shared_id']
    prekey_id_str = row['transit_prekey_id'] if isinstance(row['transit_prekey_id'], str) else crypto.b64encode(row['transit_prekey_id']) if row['transit_prekey_id'] else 'NULL'
    print(f"  peer_id={peer_id_short} (self={is_self}) prekey_id={prekey_id_str[:20]}...")

# Now Device 2 joins (this should trigger the bootstrap fix)
print("\n=== Device 2 joins (bootstrap fix should run) ===")
alice_device2 = link.join(link_url=invite_link, t_ms=5000, db=db)
print(f"Device 2 peer_id: {alice_device2['peer_id'][:20]}...")
print(f"Device 2 peer_shared_id: {alice_device2['peer_shared_id'][:20]}...")
db.commit()

# Check Device 1's transit_prekeys_shared AFTER Device 2 joins
print("\n=== Device 1's transit_prekeys_shared AFTER Device 2 joins ===")
dev1_prekeys_after = safedb1.query(
    "SELECT peer_id, transit_prekey_id FROM transit_prekeys_shared WHERE recorded_by = ?",
    (alice_device1['peer_id'],)
)
print(f"Device 1 has {len(dev1_prekeys_after)} transit_prekeys_shared:")
for row in dev1_prekeys_after:
    peer_id_short = row['peer_id'][:10] + '...' if row['peer_id'] else 'NULL'
    is_self = row['peer_id'] == alice_device1['peer_shared_id']
    is_device2 = row['peer_id'] == alice_device2['peer_shared_id']
    label = "SELF" if is_self else ("DEVICE2" if is_device2 else "OTHER")
    prekey_id_str = row['transit_prekey_id'] if isinstance(row['transit_prekey_id'], str) else crypto.b64encode(row['transit_prekey_id']) if row['transit_prekey_id'] else 'NULL'
    print(f"  peer_id={peer_id_short} ({label}) prekey_id={prekey_id_str[:20]}...")

# Check if Device 1 can get Device 2's prekey via get_transit_prekey_for_peer
print("\n=== Can Device 1 get Device 2's prekey? ===")
dev2_prekey_for_dev1 = transit_prekey.get_transit_prekey_for_peer(
    alice_device2['peer_shared_id'],
    alice_device1['peer_id'],
    db
)
if dev2_prekey_for_dev1:
    print(f"✓ Device 1 CAN get Device 2's prekey via get_transit_prekey_for_peer()")
    print(f"  prekey_id={crypto.b64encode(dev2_prekey_for_dev1['id'])[:20]}...")
else:
    print(f"✗ Device 1 CANNOT get Device 2's prekey via get_transit_prekey_for_peer()")

# Check if Device 2 can get Device 1's prekey
print("\n=== Can Device 2 get Device 1's prekey? ===")
dev1_prekey_for_dev2 = transit_prekey.get_transit_prekey_for_peer(
    alice_device1['peer_shared_id'],
    alice_device2['peer_id'],
    db
)
if dev1_prekey_for_dev2:
    print(f"✓ Device 2 CAN get Device 1's prekey via get_transit_prekey_for_peer()")
    print(f"  prekey_id={crypto.b64encode(dev1_prekey_for_dev2['id'])[:20]}...")
else:
    print(f"✗ Device 2 CANNOT get Device 1's prekey via get_transit_prekey_for_peer()")
