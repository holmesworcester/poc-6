"""Debug: Check if Device 2's transit_prekey_shared is created and synced."""
import sqlite3
from db import Database
import schema
from events.identity import user, link_invite, link
import tick

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

print("\n=== Device 1: Create Network ===")
alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
device1_peer_id = alice_device1['peer_id']
device1_peer_shared_id = alice_device1['peer_shared_id']
print(f"Device 1: peer_id={device1_peer_id[:20]}...")
print(f"Device 1: peer_shared_id={device1_peer_shared_id[:20]}...")

db.commit()

print("\n=== Device 1: Create Link Invite ===")
invite_id, invite_link, invite_data = link_invite.create(
    peer_id=device1_peer_id,
    t_ms=2000,
    db=db
)
print(f"Link invite created: {invite_id[:20]}...")

db.commit()

print("\n=== Device 2: Join via Link ===")
alice_device2 = link.join(
    link_url=invite_link,
    t_ms=3000,
    db=db
)
device2_peer_id = alice_device2['peer_id']
device2_peer_shared_id = alice_device2['peer_shared_id']
print(f"Device 2: peer_id={device2_peer_id[:20]}...")
print(f"Device 2: peer_shared_id={device2_peer_shared_id[:20]}...")

db.commit()

print("\n=== Check: Device 2's Transit Prekey Events ===")
# Check if transit_prekey was created
d2_transit_prekeys = db.query(
    "SELECT transit_prekey_id FROM transit_prekeys WHERE owner_peer_id = ?",
    (device2_peer_id,)
)
print(f"Device 2 has {len(d2_transit_prekeys)} transit_prekey(s)")
for pk in d2_transit_prekeys:
    print(f"  transit_prekey_id: {pk['transit_prekey_id'][:20]}...")

# Check if transit_prekey_shared was created
# This should be in recorded table as a shareable event
d2_transit_prekey_shared_events = db.query(
    """SELECT r.ref_id, r.recorded_by
       FROM recorded r
       JOIN store s ON r.ref_id = s.id
       WHERE r.recorded_by = ?
       AND s.blob LIKE '%transit_prekey_shared%'""",
    (device2_peer_id,)
)
print(f"\nDevice 2 has {len(d2_transit_prekey_shared_events)} transit_prekey_shared event(s)")
for ev in d2_transit_prekey_shared_events:
    print(f"  event_id: {ev['ref_id'][:20]}...")

# Check if it's marked as shareable
if d2_transit_prekey_shared_events:
    event_id = d2_transit_prekey_shared_events[0]['ref_id']
    shareable = db.query(
        "SELECT * FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (event_id, device2_peer_id)
    )
    print(f"\n  Marked as shareable by Device 2? {len(shareable) > 0}")
    if shareable:
        print(f"    window_id: {shareable[0]['window_id']}")
        print(f"    created_at: {shareable[0]['created_at']}")
        print(f"    recorded_at: {shareable[0]['recorded_at']}")

print("\n=== Run 1 Tick (allow sync to happen) ===")
tick.tick(t_ms=4000, db=db)

print("\n=== Check: Device 1 Received Device 2's Transit Prekey? ===")
# Check if Device 1 has Device 2's transit_prekey_shared in recorded table
d1_sees_d2_events = db.query(
    """SELECT r.ref_id, r.recorded_by
       FROM recorded r
       JOIN store s ON r.ref_id = s.id
       WHERE r.recorded_by = ?
       AND s.blob LIKE '%transit_prekey_shared%'
       AND r.ref_id IN (
           SELECT ref_id FROM recorded WHERE recorded_by = ?
       )""",
    (device1_peer_id, device2_peer_id)
)
print(f"Device 1 has {len(d1_sees_d2_events)} transit_prekey_shared events from Device 2")

# Check if Device 1 has Device 2 in transit_prekeys_shared table
d1_has_d2_prekey = db.query(
    "SELECT transit_prekey_id, peer_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (device2_peer_shared_id, device1_peer_id)
)
print(f"\nDevice 1 sees {len(d1_has_d2_prekey)} transit_prekey(s) for Device 2 in transit_prekeys_shared:")
for pk in d1_has_d2_prekey:
    print(f"  peer_id: {pk['peer_id'][:20]}...")
    print(f"  transit_prekey_id: {pk['transit_prekey_id'][:20]}...")

if not d1_has_d2_prekey:
    print("\n❌ Device 1 does NOT have Device 2's transit_prekey!")
    print("   This is why Device 1 cannot send sync_connect to Device 2")

    # Debug: Check sync_connections to see if Device 2 sent sync_connect to Device 1
    connections = db.query("SELECT peer_shared_id FROM sync_connections WHERE peer_shared_id = ?", (device2_peer_shared_id,))
    if connections:
        print(f"\n   Device 2 DID send sync_connect to Device 1 (found in sync_connections)")
    else:
        print(f"\n   Device 2 did NOT send sync_connect to Device 1")
else:
    print("\n✅ Device 1 HAS Device 2's transit_prekey!")
    print("   Device 1 should be able to send sync_connect to Device 2")
