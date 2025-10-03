#!/usr/bin/env python3
"""Debug script to check recorded events for invite_key_shared."""
import sqlite3
import logging
from db import Database
import schema
from events import user, sync, message
import crypto

# Enable INFO logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')

conn = sqlite3.Connection(':memory:')
db = Database(conn)
schema.create_all(db)

# Run the exact test scenario
alice = user.new_network(name='Alice', t_ms=1000, db=db)
bob = user.join(invite_link=alice['invite_link'], name='Bob', t_ms=2000, db=db)
charlie = user.new_network(name='Charlie', t_ms=3000, db=db)

user.send_bootstrap_events(
    peer_id=bob['peer_id'],
    peer_shared_id=bob['peer_shared_id'],
    user_id=bob['user_id'],
    prekey_shared_id=bob['prekey_shared_id'],
    invite_data=bob['invite_data'],
    t_ms=4000,
    db=db
)

sync.receive(batch_size=20, t_ms=4100, db=db)
sync.receive(batch_size=20, t_ms=4200, db=db)
sync.receive(batch_size=20, t_ms=4300, db=db)
sync.sync_all(t_ms=4400, db=db)
sync.receive(batch_size=20, t_ms=4500, db=db)
sync.receive(batch_size=20, t_ms=4600, db=db)
sync.sync_all(t_ms=4700, db=db)
sync.receive(batch_size=20, t_ms=4800, db=db)
sync.receive(batch_size=20, t_ms=4900, db=db)
db.commit()

message.create_message(
    params={
        'content': 'Hello from Alice',
        'channel_id': alice['channel_id'],
        'group_id': alice['group_id'],
        'peer_id': alice['peer_id'],
        'peer_shared_id': alice['peer_shared_id'],
        'key_id': alice['key_id']
    },
    t_ms=5000,
    db=db
)

message.create_message(
    params={
        'content': 'Hello from Bob',
        'channel_id': alice['channel_id'],
        'group_id': alice['group_id'],
        'peer_id': bob['peer_id'],
        'peer_shared_id': bob['peer_shared_id'],
        'key_id': bob['key_id']
    },
    t_ms=6000,
    db=db
)

sync.sync_all(t_ms=8000, db=db)
sync.receive(batch_size=10, t_ms=9000, db=db)
sync.receive(batch_size=100, t_ms=10000, db=db)
sync.sync_all(t_ms=11000, db=db)
sync.receive(batch_size=10, t_ms=12000, db=db)
sync.receive(batch_size=100, t_ms=13000, db=db)

# Find invite_key_shared event
import base64, json
invite_code = alice['invite_link'].split('/')[-1]
invite_data = json.loads(base64.urlsafe_b64decode(invite_code + '=' * ((4 - len(invite_code) % 4) % 4)))
invite_key_shared_id = invite_data['invite_key_shared_id']

print(f"invite_key_shared_id: {invite_key_shared_id}")

# Find all recorded events for this invite_key_shared
all_events = db.query('SELECT id, blob FROM store')
recorded_events = []
for row in all_events:
    try:
        data = crypto.parse_json(row['blob'])
        if data.get('type') == 'recorded' and data.get('ref_id') == invite_key_shared_id:
            fs_id = crypto.b64encode(row['id'])
            seen_by = data['seen_by']
            recorded_events.append((fs_id, seen_by))
    except:
        pass

print(f"\nFound {len(recorded_events)} recorded events for invite_key_shared:")
for fs_id, seen_by in recorded_events:
    if seen_by == alice['peer_id']:
        print(f"  - seen_by Alice: {fs_id}")
    elif seen_by == bob['peer_id']:
        print(f"  - seen_by Bob: {fs_id}")
    else:
        print(f"  - seen_by Unknown ({seen_by[:20]}...): {fs_id}")

# Check invite_keys_shared table BEFORE convergence tests
rows = db.query('SELECT * FROM invite_keys_shared')
print(f"\ninvite_keys_shared table has {len(rows)} rows BEFORE convergence tests:")
for row in rows:
    seen_by = row['recorded_by']
    if seen_by == alice['peer_id']:
        print(f"  - seen_by Alice")
    elif seen_by == bob['peer_id']:
        print(f"  - seen_by Bob")
    else:
        print(f"  - seen_by Unknown ({seen_by[:20]}...)")

# Run convergence tests (this is where the extra row appears!)
print("\n=== Running idempotency test ===")
from tests.utils import assert_idempotency
assert_idempotency(db, num_trials=1, max_repetitions=2)

# Check again AFTER idempotency
rows = db.query('SELECT * FROM invite_keys_shared')
print(f"\ninvite_keys_shared table has {len(rows)} rows AFTER idempotency:")
for row in rows:
    seen_by = row['recorded_by']
    if seen_by == alice['peer_id']:
        print(f"  - seen_by Alice")
    elif seen_by == bob['peer_id']:
        print(f"  - seen_by Bob")
    else:
        print(f"  - seen_by Unknown ({seen_by[:20]}...)")
