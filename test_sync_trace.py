"""Trace sync request/response flow to find where it breaks."""
import sqlite3
import logging
from db import Database
import schema
from events.identity import user, link_invite, link
import tick

# Enable all sync-related logging
logging.basicConfig(level=logging.DEBUG, format='%(message)s')

conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

print("\n" + "="*80)
print("SETUP: Create Device 1 (network creator)")
print("="*80)
alice_device1 = user.new_network(name='Alice', t_ms=1000, db=db)
device1_peer_id = alice_device1['peer_id']
device1_peer_shared_id = alice_device1['peer_shared_id']
print(f"Device 1: peer_id={device1_peer_id[:20]}...")
print(f"Device 1: peer_shared_id={device1_peer_shared_id[:20]}...")

db.commit()

print("\n" + "="*80)
print("SETUP: Device 1 creates link invite")
print("="*80)
invite_id, invite_link, invite_data = link_invite.create(
    peer_id=device1_peer_id,
    t_ms=2000,
    db=db
)
print(f"Invite created: {invite_id[:20]}...")

db.commit()

print("\n" + "="*80)
print("SETUP: Device 2 joins via link")
print("="*80)
alice_device2 = link.join(
    link_url=invite_link,
    t_ms=3000,
    db=db
)
device2_peer_id = alice_device2['peer_id']
device2_peer_shared_id = alice_device2['peer_shared_id']
device2_transit_prekey_shared_id = alice_device2['transit_prekey_shared_id']
print(f"Device 2: peer_id={device2_peer_id[:20]}...")
print(f"Device 2: peer_shared_id={device2_peer_shared_id[:20]}...")
print(f"Device 2: transit_prekey_shared_id={device2_transit_prekey_shared_id[:20]}...")

db.commit()

# Check initial state
print("\n" + "="*80)
print("STATE CHECK: Before any ticks")
print("="*80)

incoming = db.query("SELECT COUNT(*) as c FROM incoming_blobs")
print(f"incoming_blobs: {incoming[0]['c']}")

connections = db.query("SELECT peer_shared_id FROM sync_connections")
print(f"sync_connections: {len(connections)} entries")
for conn in connections:
    print(f"  - {conn['peer_shared_id'][:20]}...")

# Tick #1: Send sync_connect
print("\n" + "="*80)
print("TICK #1 (t=4000): Should send sync_connect")
print("="*80)
tick.tick(t_ms=4000, db=db)
db.commit()

incoming = db.query("SELECT id, deliver_at FROM incoming_blobs")
print(f"\nincoming_blobs after tick #1: {len(incoming)} messages")
for msg in incoming:
    print(f"  - id={msg['id']} deliver_at={msg['deliver_at']}")

connections = db.query("SELECT peer_shared_id FROM sync_connections")
print(f"\nsync_connections after tick #1: {len(connections)} entries")
for conn in connections:
    match = "Device 1" if conn['peer_shared_id'] == device1_peer_shared_id else ("Device 2" if conn['peer_shared_id'] == device2_peer_shared_id else "Unknown")
    print(f"  - {conn['peer_shared_id'][:20]}... ({match})")

# Tick #2: Receive sync_connect, send sync requests
print("\n" + "="*80)
print("TICK #2 (t=4100): Should receive sync_connect and send sync requests")
print("="*80)
tick.tick(t_ms=4100, db=db)
db.commit()

incoming = db.query("SELECT id, deliver_at FROM incoming_blobs")
print(f"\nincoming_blobs after tick #2: {len(incoming)} messages")
for msg in incoming:
    print(f"  - id={msg['id']} deliver_at={msg['deliver_at']}")

# Check if Device 1 has Device 2's transit_prekey yet
d1_has_d2 = db.query(
    "SELECT transit_prekey_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (device2_peer_shared_id, device1_peer_id)
)
print(f"\nDevice 1 has Device 2's transit_prekey: {len(d1_has_d2) > 0}")

# Tick #3: Receive sync requests, send responses
print("\n" + "="*80)
print("TICK #3 (t=4200): Should receive sync requests and send responses")
print("="*80)
tick.tick(t_ms=4200, db=db)
db.commit()

incoming = db.query("SELECT id, deliver_at FROM incoming_blobs")
print(f"\nincoming_blobs after tick #3: {len(incoming)} messages")

d1_has_d2 = db.query(
    "SELECT transit_prekey_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (device2_peer_shared_id, device1_peer_id)
)
print(f"\nDevice 1 has Device 2's transit_prekey: {len(d1_has_d2) > 0}")

# Tick #4: Receive sync responses
print("\n" + "="*80)
print("TICK #4 (t=4300): Should receive sync responses")
print("="*80)
tick.tick(t_ms=4300, db=db)
db.commit()

d1_has_d2 = db.query(
    "SELECT transit_prekey_id FROM transit_prekeys_shared WHERE peer_id = ? AND recorded_by = ?",
    (device2_peer_shared_id, device1_peer_id)
)

print("\n" + "="*80)
print("FINAL RESULT")
print("="*80)
print(f"Device 1 has Device 2's transit_prekey: {len(d1_has_d2) > 0}")
if len(d1_has_d2) > 0:
    print("✅ SUCCESS")
else:
    print("❌ FAILURE - Device 1 never received Device 2's transit_prekey_shared")
