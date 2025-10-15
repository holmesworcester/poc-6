#!/usr/bin/env python3
"""Analyze a specific convergence failure."""
import json
import sys
import sqlite3
from db import Database
import schema
import crypto
import os
import glob

# Find most recent failure file
failure_files = glob.glob('tests/failures/convergence_failure_*.json')
if not failure_files:
    print("No failure files found!")
    sys.exit(1)

failure_file = max(failure_files, key=os.path.getctime)
print(f"Loading failure from: {failure_file}\n")

# Load failure
with open(failure_file, 'r') as f:
    failure = json.load(f)

# Create fresh DB
conn = sqlite3.Connection(':memory:')
db = Database(conn)
schema.create_all(db)

# Copy the store
print("Restoring store...")
for row in failure['store']:
    db.execute(
        "INSERT INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
        (row['id'], row['blob'].encode('latin1'), row['stored_at'])
    )
print(f"Restored {len(failure['store'])} store entries")

# Copy local_peers
print("Restoring local_peers...")
for row in failure['baseline_state']['local_peers']:
    db.execute(
        "INSERT INTO local_peers (peer_id, public_key, private_key, created_at) VALUES (?, ?, ?, ?)",
        (crypto.b64decode(row['peer_id']), crypto.b64decode(row['public_key']), crypto.b64decode(row['private_key']), row['created_at'])
    )
print(f"Restored {len(failure['baseline_state']['local_peers'])} local peers")

print(f"\nReplaying failed ordering ({len(failure['failed_order'])} events)...")

# Replay failed order
from events.transit import recorded

for i, event_id in enumerate(failure['failed_order']):
    # Get event type for logging
    try:
        blob = db.query_one("SELECT blob FROM store WHERE id = ?", (event_id,))
        if blob:
            data = crypto.parse_json(blob['blob'])
            event_type = data.get('type', 'unknown')

            if event_type == 'recorded':
                ref_id = data.get('ref_id')
                if ref_id:
                    ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
                    if ref_blob:
                        try:
                            ref_data = crypto.parse_json(ref_blob['blob'])
                            wrapped_type = ref_data.get('type', 'unknown')
                            recorded_by = data.get('recorded_by', 'unknown')

                            # Check if this is a channel event
                            if wrapped_type == 'channel':
                                print(f"\n[{i+1}] Projecting channel event {ref_id[:20]}... for {recorded_by[:20]}...")

                                # Check if it will be blocked
                                unwrapped, missing_keys = crypto.unwrap(ref_blob['blob'], recorded_by, db)
                                if missing_keys:
                                    print(f"  ⚠️  Will be blocked on keys: {[k[:20]+'...' for k in missing_keys]}")
                                else:
                                    print(f"  ✓ Can unwrap successfully")
                        except Exception as e:
                            pass
    except Exception as e:
        pass

    # Project the event
    recorded.project(event_id, db)

# Check final state
print("\n=== Final State ===")
channels = db.query("SELECT channel_id, name, recorded_by, recorded_at FROM channels")
print(f"Channels table: {len(channels)} rows")
for ch in channels:
    print(f"  - {ch['name']} recorded_by={ch['recorded_by'][:20]}... at {ch['recorded_at']}")

blocked = db.query("SELECT COUNT(*) as count FROM blocked_events_ephemeral")
print(f"\nBlocked events: {blocked[0]['count']}")

if blocked[0]['count'] > 0:
    print("\nBlocked events details:")
    blocked_events = db.query("SELECT recorded_id, recorded_by, missing_deps FROM blocked_events_ephemeral")
    for be in blocked_events:
        print(f"  - recorded_id={be['recorded_id'][:20]}... recorded_by={be['recorded_by'][:20]}...")
        print(f"    missing_deps: {be['missing_deps']}")

        # Try to identify what's blocked
        rec_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (be['recorded_id'],))
        if rec_blob:
            rec_data = crypto.parse_json(rec_blob['blob'])
            ref_id = rec_data.get('ref_id')
            if ref_id:
                ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
                if ref_blob:
                    try:
                        ref_data = crypto.parse_json(ref_blob['blob'])
                        print(f"    ref event type: {ref_data.get('type')}")
                    except:
                        print(f"    ref event: encrypted")
