"""Trace sync requests to see if Device 1 is requesting Device 2's transit_prekey."""
import sqlite3
import logging
from db import Database
import schema
from events.identity import user, link_invite, link
import tick

# Enable debug logging to see sync flow
logging.basicConfig(level=logging.WARNING, format='%(message)s')

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
device2_transit_prekey_shared_id = alice_device2['transit_prekey_shared_id']
print(f"peer_shared_id: {device2_peer_shared_id[:20]}...")
print(f"transit_prekey_shared_id: {device2_transit_prekey_shared_id[:20]}...")

db.commit()

# Check sync_connections before ticks
connections_before = db.query("SELECT peer_shared_id, last_seen_ms, ttl_ms FROM sync_connections")
print(f"\n=== sync_connections BEFORE tick ({len(connections_before)} entries) ===")
for conn in connections_before:
    peer_match = "Device 1" if conn['peer_shared_id'] == device1_peer_shared_id else ("Device 2" if conn['peer_shared_id'] == device2_peer_shared_id else "Unknown")
    print(f"  peer_shared_id: {conn['peer_shared_id'][:20]}... ({peer_match})")
    print(f"    last_seen_ms: {conn['last_seen_ms']}, ttl_ms: {conn['ttl_ms']}")

print("\n=== Run tick #1 (t=4000) ===")
print("Looking for [SYNC_SEND] and [SYNC_REQUEST] logs from Device 1...")
tick.tick(t_ms=4000, db=db)

# Check if Device 2's transit_prekey_shared is shareable
shareable = db.query(
    "SELECT event_id, can_share_peer_id, window_id FROM shareable_events WHERE event_id = ?",
    (device2_transit_prekey_shared_id,)
)
print(f"\n=== Device 2's transit_prekey_shared in shareable_events ===")
if shareable:
    print(f"✅ YES - shareable by peer: {shareable[0]['can_share_peer_id'][:20]}...")
    print(f"   window_id: {shareable[0]['window_id']}")
else:
    print(f"❌ NO - NOT in shareable_events!")

# Check sync_connections after tick #1
connections_after = db.query("SELECT peer_shared_id, last_seen_ms, ttl_ms FROM sync_connections")
print(f"\n=== sync_connections AFTER tick #1 ({len(connections_after)} entries) ===")
for conn in connections_after:
    peer_match = "Device 1" if conn['peer_shared_id'] == device1_peer_shared_id else ("Device 2" if conn['peer_shared_id'] == device2_peer_shared_id else "Unknown")
    print(f"  peer_shared_id: {conn['peer_shared_id'][:20]}... ({peer_match})")
    print(f"    last_seen_ms: {conn['last_seen_ms']}, ttl_ms: {conn['ttl_ms']}")
    # Check if connection is active at t=4000
    if conn['last_seen_ms'] + conn['ttl_ms'] > 4000:
        print(f"    ✅ ACTIVE at t=4000")
    else:
        print(f"    ❌ EXPIRED at t=4000")

print("\n=== Run tick #2 (t=5000) ===")
tick.tick(t_ms=5000, db=db)

print("\n=== Run tick #3 (t=6000) ===")
tick.tick(t_ms=6000, db=db)

# Final check
d1_has_d2 = db.query(
    "SELECT transit_prekey_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (device2_peer_shared_id, device1_peer_id)
)
print(f"\n=== FINAL RESULT ===")
print(f"Device 1 has Device 2's transit_prekey: {len(d1_has_d2) > 0}")
if d1_has_d2:
    print(f"  ✅ SUCCESS")
else:
    print(f"  ❌ FAILURE")
