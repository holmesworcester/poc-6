#!/usr/bin/env python3
"""Replay convergence failure with detailed assertions at each step."""
import json
import sqlite3
from db import Database
import schema
from events.transit import recorded
from db import create_safe_db

# Load failure data
with open('tests/failures/convergence_failure_1761247864_ordering_1.json', 'r') as f:
    data = json.load(f)

# Create fresh in-memory DB
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Repopulate store table with all blobs (blobs are already strings from JSON, encode as utf-8)
print("Repopulating store table...")
for row in data['store']:
    blob_val = row['blob']
    if isinstance(blob_val, str):
        blob_val = blob_val.encode('utf-8')
    db.execute("INSERT INTO store VALUES (?, ?, ?)",
               (row['id'], blob_val, row['stored_at']))
db.commit()
print(f"✓ Store has {len(data['store'])} rows\n")

# Test both orderings
orderings = {
    'baseline': data['baseline_order'],
    'failed': data['failed_order']
}

print("Testing both orderings...\n")

# Track which channel recordings we've seen
channel_recordings = {}

def recreate_projection_tables(db):
    """Recreate projection tables like convergence.py does - NOT store!"""
    import sqlite3

    # Clear ephemeral tables
    db.execute("DELETE FROM incoming_blobs")
    db.execute("DELETE FROM blocked_events_ephemeral")

    # Drop and recreate ALL EXCEPT STORE
    tables = [
        'messages', 'message_deletions', 'deleted_events',
        'peers_shared', 'peer_self', 'groups', 'channels', 'addresses',
        'group_keys', 'group_keys_shared', 'group_prekeys', 'group_prekeys_shared',
        'transit_prekeys_shared', 'users', 'group_members', 'valid_events',
        'blocked_event_deps_ephemeral', 'shareable_events', 'invites', 'networks',
        'bootstrap_status', 'files', 'file_slices', 'message_attachments',
        'keys_to_purge', 'message_rekeys', 'event_dependencies'
    ]

    for table in tables:
        try:
            db._conn.execute(f"DROP TABLE IF EXISTS {table}")
        except:
            pass

    # Recreate using schema (will skip store since it already exists)
    schema.create_all(db)
    db.commit()

for ordering_name in ['baseline', 'failed']:
    print(f"\n{'='*60}")
    print(f"Testing {ordering_name.upper()} ordering")
    print(f"{'='*60}\n")

    order = orderings[ordering_name]

    # Recreate projection tables (like convergence.py)
    recreate_projection_tables(db)

    print(f"Projecting {len(order)} recorded events from store\n")

    # Project each event
    for i, recorded_id in enumerate(order):
        try:
            recorded.project(recorded_id, db)
        except Exception as e:
            print(f"[{i+1:3d}] ERROR: {e}")
            import traceback
            traceback.print_exc()
            break

    db.commit()

    # Final check - count total channels
    all_channels = db.query("SELECT * FROM channels")
    print(f"✓ Replay complete: {len(all_channels)} channel rows in database")
    for ch in all_channels:
        print(f"  {ch['channel_id'][:15]}... by {ch['recorded_by'][:15]}...")

    # Check blocked events
    blocked = db.query("SELECT * FROM blocked_events_ephemeral")
    print(f"\nBlocked events: {len(blocked)}")

    # Get store rows to figure out what event types are blocked
    channel_blocks = []
    for b in blocked:
        recorded_id = b['recorded_id']
        try:
            recorded_blob_row = db.query_one("SELECT blob FROM store WHERE id = ?", (recorded_id,))
            if recorded_blob_row:
                import crypto
                recorded_data = crypto.parse_json(recorded_blob_row['blob'])
                ref_id = recorded_data.get('ref_id')
                if ref_id:
                    ref_blob_row = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
                    if ref_blob_row:
                        try:
                            ref_data = crypto.parse_json(ref_blob_row['blob'])
                            event_type = ref_data.get('type', 'encrypted')
                            if event_type == 'channel':
                                deps = json.loads(b['missing_deps'])
                                channel_blocks.append(b)
                                print(f"  ★ BLOCKED CHANNEL: recorded_by={b['recorded_by'][:15]}... missing {len(deps)} deps: {deps}")
                        except:
                            pass
        except:
            pass

    if not channel_blocks:
        print("  (no blocked channels)")
