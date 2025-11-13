"""Debug script to check shareable_events for messages."""
import sqlite3
from db import Database, create_safe_db
import schema
from events.identity import user, invite, peer
from events.content import message
import tick

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates a network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id'][:20]}...")

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

db.commit()

# Initial sync
for i in range(15):
    tick.tick(t_ms=4000 + i*200, db=db)

# Alice sends a message
alice_msg = message.create(
    peer_id=alice['peer_id'],
    channel_id=alice['channel_id'],
    content="Hello from Alice!",
    t_ms=5000,
    db=db
)
db.commit()
print(f"\nAlice created message: {alice_msg['id'][:20]}...")

# Bob sends a message
bob_msg = message.create(
    peer_id=bob['peer_id'],
    channel_id=bob['channel_id'],
    content="Hello from Bob!",
    t_ms=5100,
    db=db
)
db.commit()
print(f"Bob created message: {bob_msg['id'][:20]}...")

# Check shareable_events for both peers BEFORE sync
print("\n=== Shareable events BEFORE sync ===")
alice_safedb = create_safe_db(db, recorded_by=alice['peer_id'])
alice_shareable = alice_safedb.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],))
print(f"Alice has {len(alice_shareable)} shareable events")
for row in alice_shareable:
    event_id = row['event_id']
    # Try to read the event to see its type
    try:
        from db import create_unsafe_db
        import store
        import crypto
        unsafedb = create_unsafe_db(db)
        blob = store.get(event_id, db)
        if blob:
            data = crypto.parse_json(blob)
            event_type = data.get('type', 'unknown')
            print(f"  - {event_id[:20]}... type={event_type}")
    except:
        print(f"  - {event_id[:20]}... (encrypted or error)")

bob_safedb = create_safe_db(db, recorded_by=bob['peer_id'])
bob_shareable = bob_safedb.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (bob['peer_id'],))
print(f"\nBob has {len(bob_shareable)} shareable events")
for row in bob_shareable:
    event_id = row['event_id']
    try:
        from db import create_unsafe_db
        import store
        import crypto
        unsafedb = create_unsafe_db(db)
        blob = store.get(event_id, db)
        if blob:
            data = crypto.parse_json(blob)
            event_type = data.get('type', 'unknown')
            print(f"  - {event_id[:20]}... type={event_type}")
    except:
        print(f"  - {event_id[:20]}... (encrypted or error)")

# Sync messages
print("\n=== Syncing ===")
for round_num in range(9):
    tick.tick(t_ms=6000 + round_num * 100, db=db)

# Check shareable_events for both peers AFTER sync
print("\n=== Shareable events AFTER sync ===")
alice_shareable = alice_safedb.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],))
print(f"Alice has {len(alice_shareable)} shareable events")

bob_shareable = bob_safedb.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (bob['peer_id'],))
print(f"Bob has {len(bob_shareable)} shareable events")

# Check if message events are in shareable_events
print(f"\nAlice's message {alice_msg['id'][:20]}... in Alice's shareable? {any(row['event_id'] == alice_msg['id'] for row in alice_shareable)}")
print(f"Bob's message {bob_msg['id'][:20]}... in Bob's shareable? {any(row['event_id'] == bob_msg['id'] for row in bob_shareable)}")

# Check messages visible to each peer
alice_messages = message.list_messages(alice['channel_id'], alice['peer_id'], db)
alice_message_contents = [msg['content'] for msg in alice_messages]
print(f"\nAlice sees {len(alice_messages)} messages: {alice_message_contents}")

bob_messages = message.list_messages(bob['channel_id'], bob['peer_id'], db)
bob_message_contents = [msg['content'] for msg in bob_messages]
print(f"Bob sees {len(bob_messages)} messages: {bob_message_contents}")
