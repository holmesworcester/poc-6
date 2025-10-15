#!/usr/bin/env python3
"""Minimal reprojection test to debug the issue."""

import sqlite3
import logging
from db import Database
import schema
from events.identity import user, invite
from events.transit import sync
from tests.utils.convergence import assert_reprojection
import crypto

logging.basicConfig(
    level=logging.WARNING,
    format='%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s'
)

conn = sqlite3.Connection(':memory:')
db = Database(conn)
schema.create_all(db)

# Alice creates network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id'][:20]}...")

# Create invite
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
print(f"Invite created: {invite_id[:20]}...")

# Bob joins
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"Bob peer_id: {bob['peer_id'][:20]}...")

# Send bootstrap (this is what the three-player test does)
user.send_bootstrap_events(
    peer_id=bob['peer_id'],
    peer_shared_id=bob['peer_shared_id'],
    user_id=bob['user_id'],
    transit_prekey_shared_id=bob['transit_prekey_shared_id'],
    invite_data=bob['invite_data'],
    t_ms=4000,
    db=db
)

# Do sync rounds (minimal)
for t in [4100, 4200]:
    sync.receive(batch_size=20, t_ms=t, db=db)

# Check if invite_accepted recorded event exists
print("\n=== Checking for invite_accepted recorded events ===")
rows = db.query('SELECT id, blob FROM store ORDER BY rowid')
invite_accepted_recorded_events = []
for row in rows:
    try:
        data = crypto.parse_json(row['blob'])
        if data.get('type') == 'recorded':
            ref_id = data.get('ref_id')
            ref_blob = db.query_one('SELECT blob FROM store WHERE id = ?', (ref_id,))
            if ref_blob:
                ref_data = crypto.parse_json(ref_blob['blob'])
                if ref_data.get('type') == 'invite_accepted':
                    recorded_id = crypto.b64encode(row['id'])
                    invite_accepted_recorded_events.append(recorded_id)
                    print(f"Found invite_accepted recorded event: {recorded_id[:20]}...")
    except:
        pass

print(f"Total invite_accepted recorded events: {len(invite_accepted_recorded_events)}")

# Test reprojection
print("\n=== Testing Reprojection ===")
assert_reprojection(db)
print("✓ Reprojection passed!")
