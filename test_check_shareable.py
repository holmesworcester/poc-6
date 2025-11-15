"""Check if Device 2's transit_prekey_shared is marked as shareable."""
import sqlite3
from db import Database
import schema
from events.identity import user, link_invite, link

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

print("\n=== Device 1: Create Network ===")
alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
device1_peer_id = alice_device1['peer_id']
device1_peer_shared_id = alice_device1['peer_shared_id']
print(f"Device 1: peer_id={device1_peer_id[:20]}...")

db.commit()

print("\n=== Device 1: Create Link Invite ===")
invite_id, invite_link, invite_data = link_invite.create(
    peer_id=device1_peer_id,
    t_ms=2000,
    db=db
)

db.commit()

print("\n=== Device 2: Join via Link ===")
alice_device2 = link.join(
    link_url=invite_link,
    t_ms=3000,
    db=db
)
device2_peer_id = alice_device2['peer_id']
device2_peer_shared_id = alice_device2['peer_shared_id']
device2_transit_prekey_shared_id = alice_device2['transit_prekey_shared_id']
print(f"Device 2: peer_id={device2_peer_id[:20]}...")
print(f"Device 2: transit_prekey_shared_id={device2_transit_prekey_shared_id[:20]}...")

db.commit()

# Check if Device 2's transit_prekey_shared is in shareable_events
shareable = db.query(
    "SELECT event_id, can_share_peer_id, window_id FROM shareable_events WHERE event_id = ?",
    (device2_transit_prekey_shared_id,)
)

print(f"\n=== Device 2's transit_prekey_shared in shareable_events ===")
if shareable:
    print(f"✅ YES - marked as shareable by {len(shareable)} peer(s):")
    for row in shareable:
        print(f"  can_share_peer_id: {row['can_share_peer_id'][:20]}...")
        print(f"  window_id: {row['window_id']}")

    # Check if it's Device 2 who can share it
    d2_can_share = [r for r in shareable if r['can_share_peer_id'] == device2_peer_id]
    if d2_can_share:
        print(f"  ✅ Device 2 CAN share this event")
    else:
        print(f"  ❌ Device 2 CANNOT share this event (wrong peer)")
else:
    print(f"❌ NO - NOT marked as shareable!")
    print(f"   This is why it won't be synced to Device 1")

# Also check Device 1's transit_prekey_shared for comparison
d1_transit_prekey_shared = db.query(
    """SELECT event_id FROM shareable_events
       WHERE can_share_peer_id = ?
       AND event_id IN (
           SELECT ref_id FROM recorded WHERE recorded_by = ? AND ref_id LIKE '%' LIMIT 100
       )""",
    (device1_peer_id, device1_peer_id)
)
print(f"\n=== Device 1 has {len(d1_transit_prekey_shared)} shareable events ===")
