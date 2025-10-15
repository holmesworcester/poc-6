#!/usr/bin/env python3
"""Debug reprojection issue."""

import sqlite3
import logging
from db import Database
import schema
from events.transit import sync
from events.identity import user, invite
import crypto

# Enable INFO logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s'
)

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice's network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id'][:20]}...")
print(f"Alice channel_id: {alice['channel_id'][:20]}...")
print(f"Alice group_id: {alice['group_id'][:20]}...")
print(f"Alice key_id: {alice['key_id'][:20]}...")

# Create invite
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
print(f"\nInvite created: {invite_id[:20]}...")

# Bob joins
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"\nBob peer_id: {bob['peer_id'][:20]}...")

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

print("\n=== Checking Bob's group_key_shared events ===")

# Check all events in store
all_events = db.query("SELECT id, blob FROM store")
group_key_shared_for_bob = []

for row in all_events:
    try:
        data = crypto.parse_json(row['blob'])
        if data.get('type') == 'recorded':
            ref_id = data.get('ref_id')
            recorded_by = data.get('recorded_by')
            if recorded_by == bob['peer_id']:
                # Check if the ref is a group_key_shared event
                ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (crypto.b64decode(ref_id),))
                if ref_blob:
                    try:
                        ref_data = crypto.parse_json(ref_blob['blob'])
                        if ref_data.get('type') == 'group_key_shared':
                            recorded_id = crypto.b64encode(row['id'])
                            group_key_shared_for_bob.append({
                                'recorded_id': recorded_id,
                                'ref_id': ref_id,
                                'key_id': ref_data.get('key_id', 'N/A')
                            })
                            print(f"Found group_key_shared: ref_id={ref_id[:20]}... key_id={ref_data.get('key_id', 'N/A')[:20]}...")
                    except Exception as e:
                        pass
    except:
        pass

print(f"\nBob has {len(group_key_shared_for_bob)} group_key_shared recorded events")

# Check if they're valid
print("\nChecking validity:")
for gks in group_key_shared_for_bob:
    is_valid = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (gks['ref_id'], bob['peer_id'])
    )
    print(f"  ref_id={gks['ref_id'][:20]}... valid={is_valid is not None}")

# Check if Bob has the key in group_keys table
print(f"\nChecking if Bob has Alice's network key ({alice['key_id'][:20]}...) in group_keys:")
bob_has_network_key = db.query_one(
    "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
    (alice['key_id'], bob['peer_id'])
)
print(f"Bob has network key: {bob_has_network_key is not None}")

# Now try reprojection
print("\n=== REPROJECTION TEST ===")
from tests.utils.convergence import _get_projectable_event_ids, _dump_projection_state, _recreate_projection_tables, _replay_events

event_ids = _get_projectable_event_ids(db)
print(f"Found {len(event_ids)} recorded events to replay")

# Capture baseline
baseline = _dump_projection_state(db)
print(f"Baseline channels: {len(baseline['channels'])} rows")

# Wipe and replay
_recreate_projection_tables(db)
_replay_events(event_ids, db)

# Check result
after = _dump_projection_state(db)
print(f"After reprojection channels: {len(after['channels'])} rows")

# Check blocked events
blocked = db.query("SELECT * FROM blocked_events_ephemeral")
print(f"\nBlocked events: {len(blocked)}")

if blocked:
    print("\nBlocked event details:")
    for b in blocked:
        try:
            recorded_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (crypto.b64decode(b['recorded_id']),))
            if recorded_blob:
                recorded_data = crypto.parse_json(recorded_blob['blob'])
                ref_id = recorded_data.get('ref_id', 'N/A')
                ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (crypto.b64decode(ref_id),))
                event_type = "unknown"
                if ref_blob:
                    try:
                        ref_data = crypto.parse_json(ref_blob['blob'])
                        event_type = ref_data.get('type', 'unknown')
                    except:
                        event_type = "encrypted"

                print(f"  type={event_type} ref_id={ref_id[:20]}... recorded_by={b['recorded_by'][:20]}...")
                print(f"    missing_deps: {[d[:20] + '...' for d in crypto.parse_json(b['missing_deps'])]}")
        except Exception as e:
            print(f"  Error parsing: {e}")

# Check if Bob has network key after reprojection
print(f"\nAfter reprojection, Bob has network key: {db.query_one('SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?', (alice['key_id'], bob['peer_id'])) is not None}")
