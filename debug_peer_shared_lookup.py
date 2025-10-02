#!/usr/bin/env python3
"""Debug peer_shared lookup issue."""

import sqlite3
from db import Database
import schema
from events import peer, key, group, sync, prekey, first_seen
import store
import crypto

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice and Bob
alice_peer_id, alice_peer_shared_id = peer.create(t_ms=1000, db=db)
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)

print(f"Alice: peer_id={alice_peer_id[:16]}... peer_shared_id={alice_peer_shared_id[:16]}...")
print(f"Bob: peer_id={bob_peer_id[:16]}... peer_shared_id={bob_peer_shared_id[:16]}...")

# Check what's in the store for peer_shared events
print("\n=== Checking peer_shared events in store ===")
alice_ps_blob = store.get(alice_peer_shared_id, db)
if alice_ps_blob:
    alice_ps_data = crypto.parse_json(alice_ps_blob)
    print(f"Alice's peer_shared event:")
    print(f"  type: {alice_ps_data.get('type')}")
    print(f"  peer_id field: {alice_ps_data.get('peer_id', 'MISSING')[:16]}...")
    print(f"  matches alice_peer_id? {alice_ps_data.get('peer_id') == alice_peer_id}")

bob_ps_blob = store.get(bob_peer_shared_id, db)
if bob_ps_blob:
    bob_ps_data = crypto.parse_json(bob_ps_blob)
    print(f"Bob's peer_shared event:")
    print(f"  type: {bob_ps_data.get('type')}")
    print(f"  peer_id field: {bob_ps_data.get('peer_id', 'MISSING')[:16]}...")
    print(f"  matches bob_peer_id? {bob_ps_data.get('peer_id') == bob_peer_id}")

# Now simulate pre-sharing Alice's peer_shared with Bob
print("\n=== Pre-sharing Alice's peer_shared with Bob ===")
alice_sees_bob = store.blob(bob_ps_blob, 3000, True, db)
bob_ps_fs = first_seen.create(alice_sees_bob, alice_peer_id, 3000, db, True)
first_seen.project(bob_ps_fs, db)

bob_sees_alice = store.blob(alice_ps_blob, 3100, True, db)
alice_ps_fs = first_seen.create(bob_sees_alice, bob_peer_id, 3100, db, True)
first_seen.project(alice_ps_fs, db)

# Check peers_shared table
print("\n=== Checking peers_shared table ===")
peers_shared_rows = db.query("SELECT * FROM peers_shared ORDER BY seen_by_peer_id")
for row in peers_shared_rows:
    print(f"seen_by: {row['seen_by_peer_id'][:16]}... peer_shared_id: {row['peer_shared_id'][:16]}...")

# Now test the lookup logic from sync.py
print("\n=== Testing sync.py lookup logic ===")
for test_peer_id in [alice_peer_id, bob_peer_id]:
    print(f"\nLooking up peer_shared_id for peer {test_peer_id[:16]}...")

    from_peer_shared_id = ""
    candidate_rows = db.query(
        "SELECT peer_shared_id FROM peers_shared WHERE seen_by_peer_id = ?",
        (test_peer_id,)
    )
    print(f"  Found {len(candidate_rows)} candidates in peers_shared")

    for row in candidate_rows:
        ps_id = row['peer_shared_id']
        print(f"  Checking candidate {ps_id[:16]}...")
        try:
            ps_blob = store.get(ps_id, db)
            if not ps_blob:
                print(f"    - Not found in store")
                continue
            ps_data = crypto.parse_json(ps_blob)
            event_peer_id = ps_data.get('peer_id', '')
            print(f"    - Event has peer_id={event_peer_id[:16] if event_peer_id else 'MISSING'}...")
            print(f"    - Matches test_peer_id? {event_peer_id == test_peer_id}")
            if ps_data.get('type') == 'peer_shared' and event_peer_id == test_peer_id:
                from_peer_shared_id = ps_id
                print(f"    ✓ FOUND MATCH!")
                break
        except Exception as e:
            print(f"    - Error: {e}")

    if from_peer_shared_id:
        print(f"  Result: peer_shared_id = {from_peer_shared_id[:16]}...")
    else:
        print(f"  Result: NO peer_shared_id found!")