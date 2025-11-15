"""Simple diagnostic: Check transit_prekey exchange in link flow."""
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
print(f"peer_shared_id: {device1_peer_shared_id[:20]}...")

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
print(f"peer_shared_id: {device2_peer_shared_id[:20]}...")

db.commit()

print("\n=== BEFORE tick() ===")
# Check if Device 1 has Device 2's transit_prekey
d1_has_d2 = db.query(
    "SELECT transit_prekey_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (device2_peer_shared_id, device1_peer_id)
)
print(f"Device 1 has Device 2's transit_prekey: {len(d1_has_d2) > 0}")

# Check if Device 2 has Device 1's transit_prekey
d2_has_d1 = db.query(
    "SELECT transit_prekey_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (device1_peer_shared_id, device2_peer_id)
)
print(f"Device 2 has Device 1's transit_prekey: {len(d2_has_d1) > 0}")

# Check incoming queue before tick
incoming_count = db.query("SELECT COUNT(*) as c FROM incoming_blobs")
print(f"\nincoming_blobs queue has {incoming_count[0]['c']} message(s) before tick")
if incoming_count[0]['c'] > 0:
    print("  Listing incoming messages:")
    incoming = db.query("SELECT id, deliver_at, dropped FROM incoming_blobs LIMIT 5")
    for msg in incoming:
        print(f"    id={msg['id']} deliver_at={msg['deliver_at']} dropped={msg['dropped']}")

print("\n=== Run tick #1 (bootstrap) ===")
tick.tick(t_ms=4000, db=db)

print("\n=== AFTER tick #1 ===")
# Check again
d1_has_d2 = db.query(
    "SELECT transit_prekey_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (device2_peer_shared_id, device1_peer_id)
)
print(f"Device 1 has Device 2's transit_prekey: {len(d1_has_d2) > 0}")
if d1_has_d2:
    print(f"  transit_prekey_id: {d1_has_d2[0]['transit_prekey_id'][:20]}...")

print("\n=== Run tick #2 ===")
tick.tick(t_ms=5000, db=db)

print("\n=== AFTER tick #2 ===")
d1_has_d2 = db.query(
    "SELECT transit_prekey_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (device2_peer_shared_id, device1_peer_id)
)
print(f"Device 1 has Device 2's transit_prekey: {len(d1_has_d2) > 0}")
if d1_has_d2:
    print(f"  transit_prekey_id: {d1_has_d2[0]['transit_prekey_id'][:20]}...")

print("\n=== Run tick #3 ===")
tick.tick(t_ms=6000, db=db)

print("\n=== AFTER tick #3 ===")
# Check again
d1_has_d2 = db.query(
    "SELECT transit_prekey_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (device2_peer_shared_id, device1_peer_id)
)
print(f"Device 1 has Device 2's transit_prekey: {len(d1_has_d2) > 0}")
if d1_has_d2:
    print(f"  transit_prekey_id: {d1_has_d2[0]['transit_prekey_id'][:20]}...")

d2_has_d1 = db.query(
    "SELECT transit_prekey_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (device1_peer_shared_id, device2_peer_id)
)
print(f"Device 2 has Device 1's transit_prekey: {len(d2_has_d1) > 0}")
if d2_has_d1:
    print(f"  transit_prekey_id: {d2_has_d1[0]['transit_prekey_id'][:20]}...")

# Check sync_connections to see if sync_connect was sent
connections = db.query("SELECT peer_shared_id, response_transit_key_id FROM sync_connections")
print(f"\n=== sync_connections table ({len(connections)} entries) ===")
for conn in connections:
    print(f"  peer_shared_id: {conn['peer_shared_id'][:20]}...")
    print(f"  response_transit_key_id: {conn['response_transit_key_id'][:20]}...")

print("\n=== Summary ===")
if len(d1_has_d2) > 0 and len(d2_has_d1) > 0:
    print("✅ SUCCESS: Bidirectional transit_prekey exchange complete!")
elif len(d2_has_d1) > 0 and len(d1_has_d2) == 0:
    print("⚠️  PARTIAL: Device 2 has Device 1's key, but Device 1 doesn't have Device 2's key")
    print("   This means bootstrap/sync didn't complete")
else:
    print("❌ FAILURE: Transit_prekey exchange failed")
