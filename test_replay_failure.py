#!/usr/bin/env python3
"""Replay the exact convergence failure using the built-in replay functionality."""
import sqlite3
import json
from db import Database
import schema
from tests.utils.convergence import replay_ordering

# Load the failure data
with open('tests/failures/convergence_failure_1761247864_ordering_1.json', 'r') as f:
    failure_data = json.load(f)

# Create fresh DB
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Populate the store table with all blobs from the failure data
print("Populating store table with blobs from failure data...")
for store_row in failure_data['store']:
    blob_val = store_row['blob']
    if isinstance(blob_val, str):
        blob_val = blob_val.encode('utf-8')
    db.execute("INSERT INTO store VALUES (?, ?, ?)",
               (store_row['id'], blob_val, store_row['stored_at']))
db.commit()
print(f"✓ Store populated with {len(failure_data['store'])} blobs\n")

# Now use the replay_ordering function with the populated store
print("Replaying convergence failure...\n")
try:
    final_state = replay_ordering(
        db,
        failure_data['failed_order'],
        expected_state=failure_data['baseline_state']
    )
    print("\n✅ Replay succeeded")
except AssertionError as e:
    print(f"\n❌ Replay failed: {e}")

# Now inspect the state
print("\n" + "="*60)
print("FINAL STATE AFTER REPLAY")
print("="*60)

# Check channels
channels = db.query("SELECT * FROM channels")
print(f"\nChannels table: {len(channels)} rows")
for ch in channels:
    print(f"  {ch['channel_id'][:20]}... by {ch['recorded_by'][:20]}... at {ch['recorded_at']}")

# Check blocked events
blocked = db.query("SELECT * FROM blocked_events_ephemeral")
print(f"\nBlocked events: {len(blocked)} rows")

# Find blocked CHANNEL events specifically
import crypto
print("\nBlocked CHANNEL events:")
for b in blocked:
    recorded_id = b['recorded_id']
    try:
        recorded_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (recorded_id,))
        if recorded_blob:
            recorded_data = crypto.parse_json(recorded_blob['blob'])
            ref_id = recorded_data.get('ref_id')
            if ref_id:
                ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
                if ref_blob:
                    try:
                        ref_data = crypto.parse_json(ref_blob['blob'])
                        if ref_data.get('type') == 'channel':
                            deps = json.loads(b['missing_deps'])
                            recorded_by = b['recorded_by'][:15]
                            print(f"  ★ recorded_by={recorded_by}... waiting for: {deps}")
                    except:
                        pass
    except:
        pass

# Check valid events count
valid_events = db.query("SELECT COUNT(DISTINCT event_id) as count FROM valid_events")
print(f"\nValid events: {valid_events[0]['count']}")
