#!/usr/bin/env python3
"""Debug why channel isn't being marked as valid for Bob."""
import sqlite3
from db import Database
import schema
from events.identity import user, invite
from events.transit import sync
import crypto

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice and Bob
alice = user.new_network(name='Alice', t_ms=1000, db=db)
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

# Send bootstrap
user.send_bootstrap_events(
    peer_id=bob['peer_id'],
    peer_shared_id=bob['peer_shared_id'],
    user_id=bob['user_id'],
    transit_prekey_shared_id=bob['transit_prekey_shared_id'],
    invite_data=bob['invite_data'],
    t_ms=4000,
    db=db
)

# Sync multiple rounds
for i in range(5):
    t = 4100 + (i * 100)
    sync.receive(batch_size=20, t_ms=t, db=db)
    sync.send_request_to_all(t_ms=t + 50, db=db)

print(f"=== Checking Channel Status for Bob ===\n")
print(f"Alice channel: {alice['channel_id']}")
print(f"Bob channel: {bob['channel_id']}")
print(f"Same channel: {alice['channel_id'] == bob['channel_id']}\n")

# Check if Bob has the channel recorded
channel_recorded = db.query(
    "SELECT recorded_id, ref_id, recorded_by FROM recorded_events WHERE ref_id = ? AND recorded_by = ?",
    (bob['channel_id'], bob['peer_id'])
)

if channel_recorded:
    print(f"✓ Channel IS recorded for Bob:")
    for row in channel_recorded:
        print(f"  recorded_id: {row['recorded_id'][:30]}...")
        print(f"  ref_id: {row['ref_id'][:30]}...")
else:
    print(f"❌ Channel NOT recorded for Bob")

# Check if Bob has the channel valid
channel_valid = db.query(
    "SELECT event_id, recorded_by FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (bob['channel_id'], bob['peer_id'])
)

if channel_valid:
    print(f"\n✓ Channel IS in valid_events for Bob")
else:
    print(f"\n❌ Channel NOT in valid_events for Bob")

# Check blocked queue
blocked = db.query(
    "SELECT recorded_id, missing_deps FROM blocked_events WHERE recorded_by = ?",
    (bob['peer_id'],)
)

if blocked:
    print(f"\n⚠️  Bob has {len(blocked)} blocked events:")
    for row in blocked:
        # Check if this is the channel
        rec = db.query_one(
            "SELECT ref_id FROM recorded_events WHERE recorded_id = ?",
            (row['recorded_id'],)
        )
        if rec and rec['ref_id'] == bob['channel_id']:
            print(f"  ❌ CHANNEL IS BLOCKED!")
            print(f"     Missing deps: {row['missing_deps']}")
        else:
            ref_id = rec['ref_id'][:20] if rec else "unknown"
            print(f"  - {ref_id}... missing: {row['missing_deps']}")
else:
    print(f"\n✓ No blocked events for Bob")

# Check if Bob has the group_key
print(f"\n=== Checking Group Key ===")
group_keys = db.query(
    "SELECT key_id, owner_peer_id FROM group_keys WHERE owner_peer_id = ?",
    (bob['peer_id'],)
)

print(f"Bob has {len(group_keys)} group keys:")
for row in group_keys:
    print(f"  - {row['key_id'][:30]}...")

# Check if the channel blob is in store
print(f"\n=== Checking Store ===")
channel_blob = db.query_one(
    "SELECT id FROM store WHERE id = ?",
    (bob['channel_id'],)
)

if channel_blob:
    print(f"✓ Channel blob IS in store")
    # Try to unwrap it
    blob = db.query_one("SELECT blob FROM store WHERE id = ?", (bob['channel_id'],))['blob']
    unwrapped, missing = crypto.unwrap(blob, bob['peer_id'], db)
    if unwrapped:
        print(f"✓ Channel can be unwrapped by Bob")
        data = crypto.parse_json(unwrapped)
        print(f"  Channel name: {data.get('name')}")
        print(f"  Group ID: {data.get('group_id')[:20]}...")
    else:
        print(f"❌ Channel CANNOT be unwrapped by Bob")
        print(f"  Missing keys: {missing}")
else:
    print(f"❌ Channel blob NOT in store")
