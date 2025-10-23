"""Debug script to trace message sync between Alice and Bob."""
import sqlite3
from db import Database
import schema
from events.identity import user, invite
from events.content import message
from events.transit import sync

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id'][:20]}...")
print(f"Alice channel_id: {alice['channel_id']}")

# Alice creates invite
invite_id, invite_link, invite_data = invite.create(
    peer_id=alice['peer_id'],
    t_ms=1500,
    db=db
)
print(f"Invite ID: {invite_id[:20]}...")

# Bob joins
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"Bob peer_id: {bob['peer_id'][:20]}...")
print(f"Bob channel_id: {bob['channel_id']}")

db.commit()

# Initial sync to establish connection
print("\n=== Initial sync (5 rounds) ===")
for i in range(5):
    sync.send_request_to_all(t_ms=2100 + i*200, db=db)
    db.commit()
    sync.receive(batch_size=20, t_ms=2200 + i*200, db=db)
    db.commit()

print("Initial sync complete")

# Alice creates a message
print("\n=== Alice creates message ===")
msg_result = message.create(
    peer_id=alice['peer_id'],
    channel_id=alice['channel_id'],
    content='Test message',
    t_ms=3000,
    db=db
)
message_id = msg_result['id']
print(f"Message ID: {message_id[:20]}...")

# Check if message is in Alice's shareable_events
shareable_check = db.query_one(
    "SELECT event_id, created_at, window_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
    (message_id, alice['peer_id'])
)
print(f"Message in Alice's shareable_events: {shareable_check is not None}")
if shareable_check:
    print(f"  created_at: {shareable_check['created_at']}")
    print(f"  window_id: {shareable_check['window_id']}")

# Check if message is in Alice's valid_events
valid_check = db.query_one(
    "SELECT event_id FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (message_id, alice['peer_id'])
)
print(f"Message in Alice's valid_events: {valid_check is not None}")

db.commit()

# Single sync round
print("\n=== Sync round 1 ===")

# Check Alice's bootstrap status before sending
alice_bootstrap = db.query_one(
    "SELECT created_network, joined_network, received_sync_request FROM bootstrap_status WHERE peer_id = ? AND recorded_by = ?",
    (alice['peer_id'], alice['peer_id'])
)
print(f"Alice bootstrap before sync: created={alice_bootstrap['created_network'] if alice_bootstrap else None}, joined={alice_bootstrap['joined_network'] if alice_bootstrap else None}, received={alice_bootstrap['received_sync_request'] if alice_bootstrap else None}")

bob_bootstrap = db.query_one(
    "SELECT created_network, joined_network, received_sync_request FROM bootstrap_status WHERE peer_id = ? AND recorded_by = ?",
    (bob['peer_id'], bob['peer_id'])
)
print(f"Bob bootstrap before sync: created={bob_bootstrap['created_network'] if bob_bootstrap else None}, joined={bob_bootstrap['joined_network'] if bob_bootstrap else None}, received={bob_bootstrap['received_sync_request'] if bob_bootstrap else None}")

# Check what window Alice will sync next
alice_sync_state = db.query_one(
    "SELECT last_window, w_param FROM sync_state_ephemeral WHERE from_peer_id = ? AND to_peer_id = ?",
    (alice['peer_id'], bob_peer_shared['peer_shared_id'])
)
if alice_sync_state:
    from events.transit.sync import compute_window_count
    total_windows = compute_window_count(alice_sync_state['w_param'])
    next_window = (alice_sync_state['last_window'] + 1) % total_windows
    print(f"Alice's sync state: last_window={alice_sync_state['last_window']}, w_param={alice_sync_state['w_param']}, next_window={next_window}")

print("Sending sync requests...")
sync.send_request_to_all(t_ms=4000, db=db)
db.commit()

print("Processing sync responses...")
sync.receive(batch_size=50, t_ms=4100, db=db)
db.commit()

# Check if Bob received the message
bob_messages = db.query_all(
    "SELECT message_id, content FROM messages WHERE recorded_by = ?",
    (bob['peer_id'],)
)
print(f"\nBob has {len(bob_messages)} message(s)")
for msg in bob_messages:
    print(f"  - {msg['message_id'][:20]}...: {msg['content']}")

# Check if message is in Bob's shareable_events
bob_shareable = db.query_one(
    "SELECT event_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
    (message_id, bob['peer_id'])
)
print(f"\nMessage in Bob's shareable_events: {bob_shareable is not None}")

# Check if message is in Bob's valid_events
bob_valid = db.query_one(
    "SELECT event_id FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (message_id, bob['peer_id'])
)
print(f"Message in Bob's valid_events: {bob_valid is not None}")

# Debug: Check what events were sent in the sync
print("\n=== Debug: Check sync state ===")
sync_state = db.query_all(
    "SELECT from_peer_id, to_peer_id, last_window, total_events_seen FROM sync_state_ephemeral"
)
print(f"Sync state entries: {len(sync_state)}")
for state in sync_state:
    print(f"  {state['from_peer_id'][:10]}... -> {state['to_peer_id'][:10]}...: window={state['last_window']}, events={state['total_events_seen']}")

# Debug: Check peer_shared_id mappings
print("\n=== Debug: Peer shared ID mappings ===")
print(f"Alice peer_id: {alice['peer_id']}")
alice_peer_shared = db.query_one(
    "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
    (alice['peer_id'], alice['peer_id'])
)
if alice_peer_shared:
    print(f"Alice peer_shared_id: {alice_peer_shared['peer_shared_id']}")

print(f"\nBob peer_id: {bob['peer_id']}")
bob_peer_shared = db.query_one(
    "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
    (bob['peer_id'], bob['peer_id'])
)
if bob_peer_shared:
    print(f"Bob peer_shared_id: {bob_peer_shared['peer_shared_id']}")

# Check who Alice knows about
alice_knows = db.query_all(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (alice['peer_id'],)
)
print(f"\nAlice knows {len(alice_knows)} peer(s):")
for p in alice_knows:
    print(f"  - {p['peer_shared_id']}")

# Check who Bob knows about
bob_knows = db.query_all(
    "SELECT peer_shared_id FROM peers_shared WHERE recorded_by = ?",
    (bob['peer_id'],)
)
print(f"\nBob knows {len(bob_knows)} peer(s):")
for p in bob_knows:
    print(f"  - {p['peer_shared_id']}")

if len(bob_messages) == 0:
    print("\n❌ FAILED: Bob did not receive the message")
else:
    print("\n✅ SUCCESS: Bob received the message")
