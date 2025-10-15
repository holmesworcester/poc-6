#!/usr/bin/env python3
"""Debug script to test if transit_key.create() is called during sync."""
import logging
import db
import sqlite3

# Setup database
conn = sqlite3.connect(':memory:')
conn.row_factory = sqlite3.Row
database = db.Database(conn)

import schema
schema.create_all(database)

from events.identity import user, invite

# Create Alice
alice = user.new_network(name='Alice', t_ms=1000, db=database)
print('Alice created')

# Create invite
invite_id, invite_link, invite_data = invite.create(
    peer_id=alice['peer_id'],
    t_ms=1500,
    db=database
)
print('Invite created')

# Create Bob by accepting invite
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=database)
print('Bob joined')

# Count transit_keys after Bob
transit_keys = database.query('SELECT key_id, owner_peer_id FROM transit_keys')
print(f'Transit keys after Bob join: {len(transit_keys)}')

# Now do ONE sync operation
print('\n=== Testing sync.send_request() ===')
from events.transit import sync

# Enable INFO logging for transit_key module
import events.transit.transit_key as transit_key_module
transit_key_module.log.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter('TRANSIT_KEY_LOG: %(message)s'))
transit_key_module.log.addHandler(handler)

print('\nBefore sync.send_request():')
transit_keys = database.query('SELECT key_id FROM transit_keys')
print(f'Transit keys: {len(transit_keys)}')

# Send one sync request from Bob to Alice
print('\n--- Calling sync.send_request() ---')
result = sync.send_request(
    from_peer_id=bob['peer_id'],
    from_peer_shared_id=bob['peer_shared_id'],
    to_peer_shared_id=alice['peer_shared_id'],
    t_ms=5000,
    db=database
)
print(f'send_request returned: {result}')

print('\n--- After sync.send_request() ---')
transit_keys = database.query('SELECT key_id, owner_peer_id FROM transit_keys')
print(f'Transit keys count: {len(transit_keys)}')
for tk in transit_keys:
    print(f'  - owner={tk["owner_peer_id"][:20]}...')

# Check if ephemeral transit_keys are in the store (they shouldn't be)
print('\n--- Checking store table ---')
store_events = database.query("SELECT id, blob FROM store")
transit_key_events_in_store = []
for evt in store_events:
    try:
        import crypto
        data = crypto.parse_json(evt['blob'])
        if data.get('type') == 'transit_key':
            transit_key_events_in_store.append(evt['id'])
    except:
        pass

print(f'Transit_key events in store: {len(transit_key_events_in_store)} (should be 1 - the invite transit_key)')
if len(transit_key_events_in_store) > 1:
    print('ERROR: Ephemeral transit_keys are being stored in event log!')
else:
    print('SUCCESS: Ephemeral transit_keys are NOT in event log')
