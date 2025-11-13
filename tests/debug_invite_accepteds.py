"""Debug: Check invite_accepteds table for bootstrap peer discovery."""
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

print("\n=== Setup ===")
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

# Sync
for i in range(5):
    tick.tick(t_ms=2500 + i*100, db=db)

# Create link invite
invite_id, invite_link, invite_data = link_invite.create(
    peer_id=alice_device1['peer_id'],
    t_ms=3000,
    db=db
)
print(f"Link invite created: {invite_id[:20]}...")
db.commit()

# Create Group B
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

# Sync
for i in range(5):
    tick.tick(t_ms=4000 + i*100, db=db)

# Link device 2
alice_device2 = link.join(link_url=invite_link, t_ms=5000, db=db)
print(f"Device 2 peer_id: {alice_device2['peer_id'][:20]}...")
print(f"Device 2 peer_shared_id: {alice_device2['peer_shared_id'][:20]}...")
db.commit()

# Check invite_accepteds table for BOTH devices
safedb1 = create_safe_db(db, recorded_by=alice_device1['peer_id'])
safedb2 = create_safe_db(db, recorded_by=alice_device2['peer_id'])

print("\n=== Device 1's invite_accepteds table ===")
dev1_accepteds = safedb1.query(
    "SELECT invite_id, inviter_peer_shared_id FROM invite_accepteds WHERE recorded_by = ?",
    (alice_device1['peer_id'],)
)
print(f"Device 1 has {len(dev1_accepteds)} invite_accepteds:")
for row in dev1_accepteds:
    inviter = row['inviter_peer_shared_id']
    if inviter == alice_device1['peer_shared_id']:
        print(f"  invite={row['invite_id'][:20]}... inviter=DEVICE1 (self)")
    elif inviter == alice_device2['peer_shared_id']:
        print(f"  invite={row['invite_id'][:20]}... inviter=DEVICE2")
    else:
        print(f"  invite={row['invite_id'][:20]}... inviter={inviter[:20] if inviter else 'None'}...")

print("\n=== Device 2's invite_accepteds table ===")
dev2_accepteds = safedb2.query(
    "SELECT invite_id, inviter_peer_shared_id FROM invite_accepteds WHERE recorded_by = ?",
    (alice_device2['peer_id'],)
)
print(f"Device 2 has {len(dev2_accepteds)} invite_accepteds:")
for row in dev2_accepteds:
    inviter = row['inviter_peer_shared_id']
    print(f"  invite={row['invite_id'][:20]}... inviter={inviter[:20] if inviter else 'NULL'}...")
    if inviter == alice_device1['peer_shared_id']:
        print(f"    ✓ This is DEVICE1")
    elif inviter == alice_device2['peer_shared_id']:
        print(f"    (This is DEVICE2 - self)")
    elif not inviter:
        print(f"    ✗ inviter_peer_shared_id is NULL!")

# Run one tick to see sync_connect bootstrap
print("\n=== Running 1 tick to try sync_connect ===")
tick.tick(t_ms=6000, db=db)
