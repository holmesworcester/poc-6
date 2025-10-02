import sqlite3
from db import Database
import schema
from events import peer, key, group, sync
import logging
logging.basicConfig(level=logging.INFO)

conn = sqlite3.Connection(':memory:')
db = Database(conn)
schema.create_all(db)

alice_peer_id, alice_peer_shared_id = peer.create(t_ms=1000, db=db)
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)

alice_public_key = peer.get_public_key(alice_peer_id, db)
bob_public_key = peer.get_public_key(bob_peer_id, db)

db.execute('INSERT INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)', (bob_peer_id, bob_public_key, 4000))
db.execute('INSERT INTO pre_keys (peer_id, public_key, created_at) VALUES (?, ?, ?)', (alice_peer_id, alice_public_key, 5000))
db.commit()

alice_key_id = key.create(alice_peer_id, t_ms=6000, db=db)
alice_group_id = group.create(name='Alice Group', peer_id=alice_peer_id, peer_shared_id=alice_peer_shared_id, key_id=alice_key_id, t_ms=7000, db=db)

bob_key_id = key.create(bob_peer_id, t_ms=9000, db=db)
bob_group_id = group.create(name='Bob Group', peer_id=bob_peer_id, peer_shared_id=bob_peer_shared_id, key_id=bob_key_id, t_ms=10000, db=db)

sync.send_request(to_peer_id=bob_peer_id, from_peer_id=alice_peer_id, from_peer_shared_id=alice_peer_shared_id, t_ms=18000, db=db)
sync.send_request(to_peer_id=alice_peer_id, from_peer_id=bob_peer_id, from_peer_shared_id=bob_peer_shared_id, t_ms=19000, db=db)

print('\n=== Round 1 ===')
sync.receive(batch_size=10, t_ms=22000, db=db)

print('\n=== Round 2 ===')
sync.receive(batch_size=100, t_ms=23000, db=db)

blocked = db.query('SELECT * FROM blocked_events', ())
print(f'\nBlocked events: {len(blocked)}')
for b in blocked:
    print(f'  Event {b["event_id"]} for peer {b["seen_by_peer_id"]}: missing {b["missing_deps"]}')
