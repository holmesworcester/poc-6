"""Debug script to check if sync_connect bootstrap discovery works for linked devices."""
import sqlite3
from db import Database, create_unsafe_db, create_safe_db
import schema
from events.identity import user, link_invite, link, peer as peer_module
from events.transit import sync_connect
import logging

# Enable logging to see sync_connect activity
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

print("\n=== Setup ===")
alice_phone = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Phone peer_id: {alice_phone['peer_id'][:20]}...")
print(f"Phone peer_shared_id: {alice_phone['peer_shared_id'][:20]}...")

db.commit()

link_invite_id, link_url, link_data = link_invite.create(
    peer_id=alice_phone['peer_id'],
    t_ms=2000,
    db=db
)
print(f"Link invite created")

db.commit()

alice_laptop = link.join(link_url=link_url, t_ms=3000, db=db)
print(f"Laptop peer_id: {alice_laptop['peer_id'][:20]}...")
print(f"Laptop peer_shared_id: {alice_laptop['peer_shared_id'][:20]}...")

db.commit()

# Check what sync_connect.send_connect_to_all() will discover for laptop
print("\n=== Checking laptop's peer discovery sources ===")
laptop_safedb = create_safe_db(db, recorded_by=alice_laptop['peer_id'])
unsafedb = create_unsafe_db(db)

# Source 1: peers_shared
peers_shared_rows = laptop_safedb.query(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (alice_laptop['peer_id'],)
)
print(f"1. peers_shared: {len(peers_shared_rows)} peers")
for row in peers_shared_rows:
    peer_id = row['peer_shared_id']
    print(f"   - {peer_id[:20]}...")
    if peer_id == alice_phone['peer_shared_id']:
        print(f"     (PHONE!)")

# Source 2: invite_accepteds (bootstrap)
bootstrap_rows = laptop_safedb.query(
    "SELECT inviter_peer_shared_id as peer_shared_id FROM invite_accepteds WHERE recorded_by = ?",
    (alice_laptop['peer_id'],)
)
print(f"2. invite_accepteds (bootstrap): {len(bootstrap_rows)} peers")
for row in bootstrap_rows:
    peer_id = row['peer_shared_id']
    print(f"   - {peer_id[:20]}...")
    if peer_id == alice_phone['peer_shared_id']:
        print(f"     (PHONE!)")

# Source 3: sync_connections
connection_rows = unsafedb.query(
    "SELECT peer_shared_id FROM sync_connections WHERE last_seen_ms + ttl_ms > ?",
    (3000,)
)
print(f"3. sync_connections: {len(connection_rows)} connections")
for row in connection_rows:
    peer_id = row['peer_shared_id']
    print(f"   - {peer_id[:20]}...")

# Now manually call send_connect_to_all
print("\n=== Calling sync_connect.send_connect_to_all() ===")
sync_connect.send_connect_to_all(t_ms=4000, db=db)

# Check if phone received sync_connect
print("\n=== Checking if phone received sync_connect ===")
from events.transit import sync
sync.receive(batch_size=20, t_ms=4000, db=db)

# Check sync_connections after
connections_after = unsafedb.query("SELECT peer_shared_id FROM sync_connections")
print(f"\nSync connections after: {len(connections_after)}")
for conn in connections_after:
    peer_id = conn['peer_shared_id']
    print(f"  - {peer_id[:20]}...")
    if peer_id == alice_phone['peer_shared_id']:
        print(f"    (PHONE)")
    if peer_id == alice_laptop['peer_shared_id']:
        print(f"    (LAPTOP)")

# Check if phone now knows about laptop
phone_safedb = create_safe_db(db, recorded_by=alice_phone['peer_id'])
phone_knows_laptop = phone_safedb.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (alice_laptop['peer_shared_id'], alice_phone['peer_id'])
)
print(f"\nPhone knows about laptop's peer_shared: {bool(phone_knows_laptop)}")

# Now phone should be able to discover laptop via sync_connections
# Let's check what phone will discover when it sends connect
print("\n=== Checking phone's peer discovery sources ===")

peers_shared_rows_phone = phone_safedb.query(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (alice_phone['peer_id'],)
)
print(f"1. peers_shared: {len(peers_shared_rows_phone)} peers")

bootstrap_rows_phone = phone_safedb.query(
    "SELECT inviter_peer_shared_id as peer_shared_id FROM invite_accepteds WHERE recorded_by = ?",
    (alice_phone['peer_id'],)
)
print(f"2. invite_accepteds (bootstrap): {len(bootstrap_rows_phone)} peers")

connection_rows_phone = unsafedb.query(
    "SELECT peer_shared_id FROM sync_connections WHERE last_seen_ms + ttl_ms > ?",
    (4100,)
)
print(f"3. sync_connections: {len(connection_rows_phone)} connections")
for row in connection_rows_phone:
    peer_id = row['peer_shared_id']
    print(f"   - {peer_id[:20]}...")
    if peer_id == alice_laptop['peer_shared_id']:
        print(f"     (LAPTOP! Phone can now discover laptop)")

print("\n=== Phone sending connect back ===")
sync_connect.send_connect_to_all(t_ms=4100, db=db)
sync.receive(batch_size=20, t_ms=4100, db=db)

# Final check
connections_final = unsafedb.query("SELECT peer_shared_id FROM sync_connections")
print(f"\nFinal sync connections: {len(connections_final)}")
for conn in connections_final:
    peer_id = conn['peer_shared_id']
    print(f"  - {peer_id[:20]}...")
    if peer_id == alice_phone['peer_shared_id']:
        print(f"    (PHONE)")
    if peer_id == alice_laptop['peer_shared_id']:
        print(f"    (LAPTOP)")
