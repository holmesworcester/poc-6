#!/usr/bin/env python3
"""Check if Bob's recorded event for Alice's channel exists."""

import sqlite3
from db import Database
import schema
from events.transit import sync
from events.identity import user, invite
import crypto

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice's network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id'][:20]}...")
print(f"Alice channel_id: {alice['channel_id'][:20]}...")

# Create invite
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)

# Bob joins
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"Bob peer_id: {bob['peer_id'][:20]}...")

# Bootstrap
user.send_bootstrap_events(
    peer_id=bob['peer_id'],
    peer_shared_id=bob['peer_shared_id'],
    user_id=bob['user_id'],
    transit_prekey_shared_id=bob['transit_prekey_shared_id'],
    invite_data=bob['invite_data'],
    t_ms=4000,
    db=db
)

# Multiple sync rounds
for t in [4100, 4150, 4200, 4300]:
    sync.receive(batch_size=20, t_ms=t, db=db)

sync.send_request_to_all(t_ms=4400, db=db)
for t in [4500, 4600]:
    sync.receive(batch_size=20, t_ms=t, db=db)

sync.send_request_to_all(t_ms=4700, db=db)
for t in [4800, 4900]:
    sync.receive(batch_size=20, t_ms=t, db=db)

# Additional rounds
for round_num in range(10):
    base_time = 15000 + (round_num * 1000)
    sync.send_request_to_all(t_ms=base_time, db=db)
    sync.receive(batch_size=100, t_ms=base_time + 100, db=db)
    sync.receive(batch_size=100, t_ms=base_time + 200, db=db)

# Check if Bob has Alice's channel projected
bob_channel = db.query(
    "SELECT * FROM channels WHERE channel_id = ? AND recorded_by = ?",
    (alice['channel_id'], bob['peer_id'])
)
print(f"\n✓ Bob has {len(bob_channel)} view(s) of Alice's channel")
if bob_channel:
    print(f"  recorded_at: {bob_channel[0]['recorded_at']}")

# Now check if there's a recorded event for it
all_recorded = db.query("SELECT id, blob FROM store")
found_recorded = []
for row in all_recorded:
    try:
        data = crypto.parse_json(row['blob'])
        if data.get('type') == 'recorded':
            ref_id = data.get('ref_id')
            recorded_by = data.get('recorded_by')
            if ref_id == alice['channel_id'] and recorded_by == bob['peer_id']:
                recorded_id = crypto.b64encode(row['id'])
                found_recorded.append(recorded_id)
                print(f"\n✓ Found recorded event: {recorded_id[:20]}...")
                print(f"  ref_id (Alice's channel): {ref_id[:20]}...")

                # Check if the channel event itself is encrypted
                channel_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
                if channel_blob:
                    try:
                        channel_data = crypto.parse_json(channel_blob['blob'])
                        print(f"  Channel event is plaintext, type: {channel_data.get('type')}")
                    except:
                        print(f"  Channel event is ENCRYPTED!")
    except:
        pass

print(f"\nTotal recorded events for Alice's channel by Bob: {len(found_recorded)}")

if len(bob_channel) > 0 and len(found_recorded) == 0:
    print("\n❌ BUG: Bob has channel projected but NO recorded event exists!")
elif len(bob_channel) == 0 and len(found_recorded) > 0:
    print("\n❌ BUG: Recorded event exists but channel not projected!")
elif len(bob_channel) > 0 and len(found_recorded) > 0:
    print("\n✓ CONSISTENT: Recorded event exists and channel is projected")
else:
    print("\n? Bob doesn't have the channel (expected during some test phases)")

# Now check: what keys does Bob have access to?
print("\n=== Checking Bob's keys ===")

# Check key_shared events that Bob has recorded
key_shared_for_bob = []
for row in all_recorded:
    try:
        data = crypto.parse_json(row['blob'])
        if data.get('type') == 'recorded':
            ref_id = data.get('ref_id')
            recorded_by = data.get('recorded_by')
            if recorded_by == bob['peer_id']:
                # Check if the ref is a key_shared event
                ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
                if ref_blob:
                    try:
                        ref_data = crypto.parse_json(ref_blob['blob'])
                        if ref_data.get('type') in ['key_shared', 'group_key_shared']:
                            key_shared_id = crypto.b64encode(row['id'])
                            key_shared_for_bob.append({
                                'recorded_id': key_shared_id,
                                'ref_id': ref_id,
                                'type': ref_data.get('type')
                            })
                    except:
                        pass
    except:
        pass

print(f"Bob has {len(key_shared_for_bob)} key_shared recorded events:")
for ks in key_shared_for_bob:
    print(f"  - {ks['type']}: recorded_id={ks['recorded_id'][:20]}... ref_id={ks['ref_id'][:20]}...")

# Check if any of these are valid for Bob
print("\nChecking which are valid for Bob:")
for ks in key_shared_for_bob:
    is_valid = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (ks['ref_id'], bob['peer_id'])
    )
    print(f"  - {ks['ref_id'][:20]}... valid: {is_valid is not None}")
