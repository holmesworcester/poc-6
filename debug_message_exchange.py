#!/usr/bin/env python3
"""Debug message exchange in three player test."""
import sqlite3
from db import Database
import schema
from events import user, message, sync

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
alice_peer_id = alice['peer_id']
alice_peer_shared_id = alice['peer_shared_id']
alice_key_id = alice['key_id']
alice_group_id = alice['group_id']
alice_channel_id = alice['channel_id']

# Bob joins
bob = user.join(invite_link=alice['invite_link'], name='Bob', t_ms=2000, db=db)
bob_peer_id = bob['peer_id']
bob_peer_shared_id = bob['peer_shared_id']
bob_user_id = bob['user_id']
bob_key_id = bob['key_id']
bob_group_id = bob['group_id']
bob_channel_id = alice_channel_id

# Bootstrap
user.send_bootstrap_events(
    peer_id=bob_peer_id,
    peer_shared_id=bob_peer_shared_id,
    user_id=bob_user_id,
    prekey_shared_id=bob['prekey_shared_id'],
    invite_data=bob['invite_data'],
    t_ms=4000,
    db=db
)

sync.receive(batch_size=20, t_ms=4100, db=db)
sync.receive(batch_size=20, t_ms=4200, db=db)
sync.receive(batch_size=20, t_ms=4300, db=db)

# Sync rounds
sync.sync_all(t_ms=4400, db=db)
sync.receive(batch_size=20, t_ms=4500, db=db)
sync.receive(batch_size=20, t_ms=4600, db=db)

sync.sync_all(t_ms=4700, db=db)
sync.receive(batch_size=20, t_ms=4800, db=db)
sync.receive(batch_size=20, t_ms=4900, db=db)

db.commit()

print("\n=== After bootstrap ===")
print(f"Alice peer_id: {alice_peer_id[:20]}...")
print(f"Bob peer_id: {bob_peer_id[:20]}...")

# Check if both have each other's peer_shared
alice_has_bob_ps = db.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (bob_peer_shared_id, alice_peer_id)
)
bob_has_alice_ps = db.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (alice_peer_shared_id, bob_peer_id)
)
print(f"Alice has Bob's peer_shared: {alice_has_bob_ps is not None}")
print(f"Bob has Alice's peer_shared: {bob_has_alice_ps is not None}")

# Check if both have the same key
alice_has_key = db.query_one("SELECT 1 FROM keys WHERE key_id = ?", (alice_key_id,))
bob_has_key = db.query_one("SELECT 1 FROM keys WHERE key_id = ?", (bob_key_id,))
print(f"Alice has key {alice_key_id[:20]}...: {alice_has_key is not None}")
print(f"Bob has key {bob_key_id[:20]}...: {bob_has_key is not None}")
print(f"Keys match: {alice_key_id == bob_key_id}")

# Check shareable events
alice_shareable = db.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (alice_peer_id,))
bob_shareable = db.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (bob_peer_id,))
print(f"\nAlice has {len(alice_shareable)} shareable events")
print(f"Bob has {len(bob_shareable)} shareable events")

# Create messages
print("\n=== Creating messages ===")
alice_msg = message.create_message(
    params={
        'content': 'Hello from Alice',
        'channel_id': alice_channel_id,
        'group_id': alice_group_id,
        'peer_id': alice_peer_id,
        'peer_shared_id': alice_peer_shared_id,
        'key_id': alice_key_id
    },
    t_ms=5000,
    db=db
)
print(f"Alice created message: {alice_msg['id'][:20]}...")

bob_msg = message.create_message(
    params={
        'content': 'Hello from Bob',
        'channel_id': bob_channel_id,
        'group_id': bob_group_id,
        'peer_id': bob_peer_id,
        'peer_shared_id': bob_peer_shared_id,
        'key_id': bob_key_id
    },
    t_ms=6000,
    db=db
)
print(f"Bob created message: {bob_msg['id'][:20]}...")

# Check if messages are shareable
alice_msg_shareable = db.query_one(
    "SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
    (alice_msg['id'], alice_peer_id)
)
bob_msg_shareable = db.query_one(
    "SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
    (bob_msg['id'], bob_peer_id)
)
print(f"Alice's message is shareable: {alice_msg_shareable is not None}")
print(f"Bob's message is shareable: {bob_msg_shareable is not None}")

# Sync messages
print("\n=== Syncing messages ===")
sync.sync_all(t_ms=11000, db=db)
sync.receive(batch_size=10, t_ms=12000, db=db)
sync.receive(batch_size=100, t_ms=13000, db=db)

# Check final state
bob_msg_valid_for_alice = db.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (bob_msg['id'], alice_peer_id)
)
alice_msg_valid_for_bob = db.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (alice_msg['id'], bob_peer_id)
)
print(f"\nBob's message valid for Alice: {bob_msg_valid_for_alice is not None}")
print(f"Alice's message valid for Bob: {alice_msg_valid_for_bob is not None}")

# Check blocked events
blocked_alice = db.query("SELECT * FROM blocked_events WHERE peer_id = ?", (alice_peer_id,))
blocked_bob = db.query("SELECT * FROM blocked_events WHERE peer_id = ?", (bob_peer_id,))
print(f"\nAlice has {len(blocked_alice)} blocked events")
print(f"Bob has {len(blocked_bob)} blocked events")

if blocked_alice:
    print("Alice's blocked events:")
    for row in blocked_alice[:3]:
        print(f"  - event_id: {row['recorded_id'][:20]}..., missing_deps: {row['missing_dep_ids']}")

if blocked_bob:
    print("Bob's blocked events:")
    for row in blocked_bob[:3]:
        print(f"  - event_id: {row['recorded_id'][:20]}..., missing_deps: {row['missing_dep_ids']}")
