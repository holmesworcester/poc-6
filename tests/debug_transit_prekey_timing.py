"""Debug: When are transit_prekey_shared events created during linking?"""
import sqlite3
from db import Database, create_unsafe_db, create_safe_db
import schema
from events.identity import user, link_invite, link
from events.group import group, group_member
import tick

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

print("\n=== Setup: Alice creates network and Group A ===")
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

print("\n=== Sync for Group A creation ===")
for i in range(5):
    tick.tick(t_ms=2500 + i*100, db=db)

print("\n=== Alice creates link invite ===")
invite_id, invite_link, invite_data = link_invite.create(
    peer_id=alice_device1['peer_id'],
    t_ms=3000,
    db=db
)
print(f"Link invite created: {invite_id[:20]}...")
db.commit()

# Check transit_prekey_shared events BEFORE linking device 2
unsafedb = create_unsafe_db(db)
safedb1 = create_safe_db(db, recorded_by=alice_device1['peer_id'])
prekeys_before = safedb1.query("SELECT transit_prekey_shared_id, peer_id FROM transit_prekeys_shared WHERE recorded_by = ?", (alice_device1['peer_id'],))
print(f"\n=== Transit prekeys BEFORE device 2 links (device 1's view): {len(prekeys_before)} ===")
for row in prekeys_before:
    print(f"  {row['transit_prekey_shared_id'][:20]}... peer_id={row['peer_id'][:20]}...")

print("\n=== Alice creates Group B (after invite) ===")
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

print("\n=== Sync for Group B creation ===")
for i in range(5):
    tick.tick(t_ms=4000 + i*100, db=db)

print("\n=== Alice links second device ===")
alice_device2 = link.join(link_url=invite_link, t_ms=5000, db=db)
print(f"Device 2 peer_id: {alice_device2['peer_id'][:20]}...")
print(f"Device 2 peer_shared_id: {alice_device2['peer_shared_id'][:20]}...")
db.commit()

# Check transit_prekey_shared events AFTER linking device 2
safedb1_after = create_safe_db(db, recorded_by=alice_device1['peer_id'])
safedb2 = create_safe_db(db, recorded_by=alice_device2['peer_id'])

prekeys_dev1 = safedb1_after.query("SELECT transit_prekey_shared_id, peer_id FROM transit_prekeys_shared WHERE recorded_by = ?", (alice_device1['peer_id'],))
prekeys_dev2 = safedb2.query("SELECT transit_prekey_shared_id, peer_id FROM transit_prekeys_shared WHERE recorded_by = ?", (alice_device2['peer_id'],))

print(f"\n=== Transit prekeys AFTER device 2 links ===")
print(f"Device 1 has {len(prekeys_dev1)} transit_prekey_shared events:")
for row in prekeys_dev1:
    print(f"  {row['transit_prekey_shared_id'][:20]}... peer_id={row['peer_id'][:20]}...")
print(f"Device 2 has {len(prekeys_dev2)} transit_prekey_shared events:")
for row in prekeys_dev2:
    print(f"  {row['transit_prekey_shared_id'][:20]}... peer_id={row['peer_id'][:20]}...")

# Check if device 1 can see device 2's prekey
print("\n=== Can device 1 see device 2's transit_prekey? ===")
from events.transit import transit_prekey
device2_prekey = transit_prekey.get_transit_prekey_for_peer(
    alice_device2['peer_shared_id'],
    alice_device1['peer_id'],
    db
)
if device2_prekey:
    print(f"✓ Device 1 CAN get transit_prekey for device 2")
else:
    print(f"✗ Device 1 CANNOT get transit_prekey for device 2")

    # Check if the transit_prekey_shared event exists
    safedb2_check = create_safe_db(db, recorded_by=alice_device2['peer_id'])
    device2_prekey_rows = safedb2_check.query(
        "SELECT transit_prekey_shared_id FROM transit_prekeys_shared WHERE recorded_by = ?",
        (alice_device2['peer_id'],)
    )
    if device2_prekey_rows:
        event_id = device2_prekey_rows[0]['transit_prekey_shared_id']
        print(f"  Device 2's transit_prekey_shared exists: {event_id[:20]}...")

        # Check if device 1 has seen it
        device1_recorded = unsafedb.query_one(
            "SELECT 1 FROM recorded WHERE event_id = ? AND recorded_by = ?",
            (event_id, alice_device1['peer_id'])
        )
        if device1_recorded:
            print(f"  ✓ Device 1 HAS recorded this event")
        else:
            print(f"  ✗ Device 1 has NOT recorded this event (not synced yet)")
    else:
        print(f"  Device 2's transit_prekey_shared DOES NOT EXIST!")

print("\n=== Run 1 tick to try initial sync ===")
tick.tick(t_ms=6000, db=db)

# Check again after tick
device2_prekey_after_tick = transit_prekey.get_transit_prekey_for_peer(
    alice_device2['peer_shared_id'],
    alice_device1['peer_id'],
    db
)
if device2_prekey_after_tick:
    print(f"✓ After 1 tick: Device 1 CAN now get transit_prekey for device 2")
else:
    print(f"✗ After 1 tick: Device 1 still CANNOT get transit_prekey for device 2")

print("\n=== Run 10 more ticks ===")
for i in range(10):
    tick.tick(t_ms=6100 + i*100, db=db)

# Check again after more ticks
device2_prekey_after_more_ticks = transit_prekey.get_transit_prekey_for_peer(
    alice_device2['peer_shared_id'],
    alice_device1['peer_id'],
    db
)
if device2_prekey_after_more_ticks:
    print(f"✓ After 11 ticks: Device 1 CAN now get transit_prekey for device 2")
else:
    print(f"✗ After 11 ticks: Device 1 still CANNOT get transit_prekey for device 2")
