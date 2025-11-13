"""Debug script to check message sync in detail."""
import sqlite3
from db import Database, create_safe_db, create_unsafe_db
import schema
from events.identity import user, invite, peer
from events.content import message
import tick
import store
import crypto
import logging

# Enable logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates a network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id'][:20]}...")
print(f"Alice channel_id: {alice['channel_id'][:20]}...")

# Alice creates an invite for Bob
invite_id, invite_link, invite_data = invite.create(
    peer_id=alice['peer_id'],
    t_ms=1500,
    db=db
)

# Bob joins Alice's network
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)
bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"Bob peer_id: {bob['peer_id'][:20]}...")
print(f"Bob channel_id: {bob['channel_id'][:20]}...")

db.commit()

# Sync so Bob gets Alice's keys
print("\n=== Initial sync (15 rounds) ===")
unsafedb = create_unsafe_db(db)
for i in range(15):
    tick.tick(t_ms=4000 + i*200, db=db)

    # Check connections after each round
    if i < 5 or i == 14:
        connections = unsafedb.query("SELECT peer_shared_id FROM sync_connections")
        print(f"  Round {i+1}: {len(connections)} connections")

    # Check if Bob has Alice's peer_shared
    if i == 0 or i == 14:
        bob_safedb_temp = create_safe_db(db, recorded_by=bob['peer_id'])
        bob_knows_alice = bob_safedb_temp.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (alice['peer_shared_id'], bob['peer_id'])
        )
        alice_safedb_temp = create_safe_db(db, recorded_by=alice['peer_id'])
        alice_knows_bob = alice_safedb_temp.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (bob['peer_shared_id'], alice['peer_id'])
        )
        print(f"    Bob knows Alice: {bool(bob_knows_alice)}, Alice knows Bob: {bool(alice_knows_bob)}")

# Check if Bob has Alice's key
bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
bob_has_alice_key = bob_safedb.query_one(
    "SELECT 1 FROM group_keys WHERE key_id = ? AND recorded_by = ?",
    (alice['key_id'], bob['peer_id'])
)
print(f"Bob has Alice's group key: {bool(bob_has_alice_key)}")

# Alice sends a message
print("\n=== Alice creates message ===")
alice_msg = message.create(
    peer_id=alice['peer_id'],
    channel_id=alice['channel_id'],
    content="Hello from Alice!",
    t_ms=5000,
    db=db
)
db.commit()
print(f"Alice message ID: {alice_msg['id'][:20]}...")

# Bob sends a message
print("\n=== Bob creates message ===")
bob_msg = message.create(
    peer_id=bob['peer_id'],
    channel_id=bob['channel_id'],
    content="Hello from Bob!",
    t_ms=5100,
    db=db
)
db.commit()
print(f"Bob message ID: {bob_msg['id'][:20]}...")

# Check if messages are in shareable_events
alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
alice_msg_shareable = alice_safedb.query_one(
    "SELECT * FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
    (alice_msg['id'], alice['peer_id'])
)
print(f"\nAlice's message in Alice's shareable_events: {bool(alice_msg_shareable)}")

bob_msg_shareable = bob_safedb.query_one(
    "SELECT * FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
    (bob_msg['id'], bob['peer_id'])
)
print(f"Bob's message in Bob's shareable_events: {bool(bob_msg_shareable)}")

# Check sync connections
unsafedb = create_unsafe_db(db)
connections = unsafedb.query("SELECT peer_shared_id FROM sync_connections")
print(f"\nSync connections before message sync: {len(connections)}")
for conn in connections:
    peer_id = conn['peer_shared_id']
    print(f"  - {peer_id[:20]}...")
    if peer_id == alice['peer_shared_id']:
        print(f"    (Alice)")
    if peer_id == bob['peer_shared_id']:
        print(f"    (Bob)")

# Sync messages (multiple rounds)
print("\n=== Syncing messages (9 rounds) ===")
for round_num in range(9):
    print(f"Round {round_num + 1}:")
    tick.tick(t_ms=6000 + round_num * 100, db=db)

    # Check messages after each round
    alice_messages = message.list_messages(alice['channel_id'], alice['peer_id'], db)
    bob_messages = message.list_messages(bob['channel_id'], bob['peer_id'], db)
    print(f"  Alice sees {len(alice_messages)} messages, Bob sees {len(bob_messages)} messages")

# Final check
print("\n=== Final message check ===")
alice_messages = message.list_messages(alice['channel_id'], alice['peer_id'], db)
alice_message_contents = [msg['content'] for msg in alice_messages]
print(f"Alice sees: {alice_message_contents}")

bob_messages = message.list_messages(bob['channel_id'], bob['peer_id'], db)
bob_message_contents = [msg['content'] for msg in bob_messages]
print(f"Bob sees: {bob_message_contents}")
