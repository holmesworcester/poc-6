"""Trace sync flow to understand why Device 1 doesn't get Device 2's prekey."""
import sqlite3
from db import Database
import schema
from events.identity import user, link_invite, link
import tick

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
device1_peer_id = alice_device1['peer_id']
device1_peer_shared_id = alice_device1['peer_shared_id']

db.commit()

invite_id, invite_link, invite_data = link_invite.create(
    peer_id=device1_peer_id,
    t_ms=2000,
    db=db
)

db.commit()

alice_device2 = link.join(
    link_url=invite_link,
    t_ms=3000,
    db=db
)
device2_peer_id = alice_device2['peer_id']
device2_peer_shared_id = alice_device2['peer_shared_id']
device2_transit_prekey_shared_id = alice_device2['transit_prekey_shared_id']

db.commit()

print(f"\nDevice 1: peer_id={device1_peer_id[:10]}...")
print(f"Device 1: peer_shared_id={device1_peer_shared_id[:10]}...")
print(f"Device 2: peer_id={device2_peer_id[:10]}...")
print(f"Device 2: peer_shared_id={device2_peer_shared_id[:10]}...")
print(f"Device 2: transit_prekey_shared_id={device2_transit_prekey_shared_id[:10]}...")

# Check Device 2's transit_prekey_shared in transit_prekeys_shared table (BEFORE ticks)
print(f"\n=== BEFORE ticks ===")
print(f"Checking transit_prekeys_shared table:")

# Device 1's view
d1_prekeys = db.query(
    "SELECT peer_id, transit_prekey_id FROM transit_prekeys_shared WHERE recorded_by = ?",
    (device1_peer_id,)
)
print(f"\nDevice 1 sees {len(d1_prekeys)} transit prekey(s):")
for pk in d1_prekeys:
    peer_match = "Device 1" if pk['peer_id'] == device1_peer_shared_id else ("Device 2" if pk['peer_id'] == device2_peer_shared_id else "Unknown")
    print(f"  peer={pk['peer_id'][:10]}... ({peer_match})")

# Device 2's view
d2_prekeys = db.query(
    "SELECT peer_id, transit_prekey_id FROM transit_prekeys_shared WHERE recorded_by = ?",
    (device2_peer_id,)
)
print(f"\nDevice 2 sees {len(d2_prekeys)} transit prekey(s):")
for pk in d2_prekeys:
    peer_match = "Device 1" if pk['peer_id'] == device1_peer_shared_id else ("Device 2" if pk['peer_id'] == device2_peer_shared_id else "Unknown")
    print(f"  peer={pk['peer_id'][:10]}... ({peer_match})")

# Run 5 ticks
for i in range(1, 6):
    print(f"\n=== Tick #{i} ===")
    tick.tick(t_ms=3000 + i*1000, db=db)

    # Check again
    d1_prekeys = db.query(
        "SELECT peer_id FROM transit_prekeys_shared WHERE recorded_by = ? AND peer_id = ?",
        (device1_peer_id, device2_peer_shared_id)
    )

    if d1_prekeys:
        print(f"✅ Device 1 NOW has Device 2's transit_prekey!")
        break
    else:
        print(f"  Device 1 still doesn't have Device 2's prekey")

print(f"\n=== FINAL CHECK ===")
# Device 1's view
d1_prekeys = db.query(
    "SELECT peer_id, transit_prekey_id FROM transit_prekeys_shared WHERE recorded_by = ?",
    (device1_peer_id,)
)
print(f"\nDevice 1 sees {len(d1_prekeys)} transit prekey(s):")
for pk in d1_prekeys:
    peer_match = "Device 1" if pk['peer_id'] == device1_peer_shared_id else ("Device 2" if pk['peer_id'] == device2_peer_shared_id else "Unknown")
    print(f"  peer={pk['peer_id'][:10]}... ({peer_match})")

if any(pk['peer_id'] == device2_peer_shared_id for pk in d1_prekeys):
    print(f"\n✅ SUCCESS: Device 1 has Device 2's transit_prekey")
else:
    print(f"\n❌ FAILURE: Device 1 does NOT have Device 2's transit_prekey after 5 ticks")
