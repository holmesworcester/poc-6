"""Debug bootstrap flow to check if invite_key_shared is shareable."""
import sqlite3
import schema
from db import Database
from events import user, sync
import crypto

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates a network
print("=== Alice creates network ===")
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id']}")
print(f"Network key_id: {alice['key_id']}")
print(f"Keys in alice dict: {list(alice.keys())}")

# Check shareable events
print("\n=== Shareable events for Alice ===")
all_shareable = db.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],))
print(f"Total shareable events for Alice: {len(all_shareable)}")

# Check blocked events
print("\n=== Blocked events for Alice ===")
blocked_events = db.query("SELECT * FROM blocked_events_ephemeral WHERE recorded_by = ?", (alice['peer_id'],))
print(f"Total blocked events for Alice: {len(blocked_events)}")
for b in blocked_events:
    print(f"  recorded_id: {b['recorded_id']}")
    print(f"    missing_deps: {b['missing_deps']}")

    # Parse the recorded event to get the ref_id
    recorded_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (crypto.b64decode(b['recorded_id']),))
    if recorded_blob:
        recorded_event = crypto.parse_json(recorded_blob['blob'])
        ref_id = recorded_event.get('ref_id')
        print(f"    ref_id (actual event): {ref_id}")

        # Check if this ref_id is shareable
        is_shareable = db.query_one("SELECT 1 FROM shareable_events WHERE event_id = ?", (ref_id,))
        print(f"    Is shareable: {bool(is_shareable)}")


# Check key_ownership for network key
print(f"\n=== key_ownership for network key ({alice['key_id']}) BEFORE Bob joins ===")
ownership = db.query("SELECT * FROM key_ownership WHERE key_id = ?", (alice['key_id'],))
print(f"Ownership entries: {len(ownership)}")
for o in ownership:
    print(f"  peer_id: {o['peer_id']}")

# Bob joins
print("\n=== Bob joins Alice's network ===")
bob = user.join(invite_link=alice['invite_link'], name='Bob', t_ms=2000, db=db)
print(f"Bob peer_id: {bob['peer_id']}")

# Bob sends bootstrap events
print("\n=== Bob sends bootstrap events ===")
user.send_bootstrap_events(
    peer_id=bob['peer_id'],
    peer_shared_id=bob['peer_shared_id'],
    user_id=bob['user_id'],
    prekey_shared_id=bob['prekey_shared_id'],
    invite_data=bob['invite_data'],
    t_ms=4000,
    db=db
)

# Alice receives Bob's bootstrap
print("\n=== Alice receives Bob's bootstrap ===")
sync.receive(batch_size=20, t_ms=4100, db=db)
sync.receive(batch_size=20, t_ms=4150, db=db)
sync.receive(batch_size=20, t_ms=4200, db=db)

# Bob receives Alice's sync response
print("\n=== Bob receives Alice's sync response ===")
sync.receive(batch_size=20, t_ms=4300, db=db)

# Check if Bob can send sync requests to Alice
print("\n=== Checking if Bob knows about Alice ===")
alice_peer_shared_for_bob = db.query_one("SELECT * FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
                                          (alice['peer_shared_id'], bob['peer_id']))
print(f"Bob knows Alice's peer_shared: {bool(alice_peer_shared_for_bob)}")
if alice_peer_shared_for_bob:
    print(f"  Alice's peer_shared_id: {alice['peer_shared_id']}")

# Check if Alice's invite_key_shared is in her shareable_events
print(f"\n=== Checking Alice's shareable events ===")
alice_shareable = db.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],))
print(f"Alice has {len(alice_shareable)} shareable events")

# Check outgoing blobs before sync
print("\n=== Checking outgoing blobs before Bob's sync ===")
outgoing_before = db.query("SELECT * FROM outgoing_blobs")
print(f"Outgoing blobs before: {len(outgoing_before)}")

# Additional bloom sync rounds (like the test)
print("\n=== Bob sends sync requests ===")
sync.sync_all(t_ms=4400, db=db)

# Check outgoing blobs after Bob's sync
outgoing_after = db.query("SELECT * FROM outgoing_blobs")
print(f"Outgoing blobs after: {len(outgoing_after)}")
print(f"Bob sent {len(outgoing_after) - len(outgoing_before)} new blobs")

# Check incoming blobs before receive
incoming_before = db.query("SELECT * FROM incoming_blobs")
print(f"\nIncoming blobs before receive: {len(incoming_before)}")

sync.receive(batch_size=20, t_ms=4500, db=db)

incoming_after = db.query("SELECT * FROM incoming_blobs")
print(f"Incoming blobs after receive: {len(incoming_after)}")

sync.receive(batch_size=20, t_ms=4600, db=db)
sync.sync_all(t_ms=4700, db=db)
sync.receive(batch_size=20, t_ms=4800, db=db)
sync.receive(batch_size=20, t_ms=4900, db=db)

bob_events_final = db.query("SELECT event_id FROM valid_events WHERE recorded_by = ?", (bob['peer_id'],))
print(f"Bob's valid events FINAL: {len(bob_events_final)}")

# Check if Bob has any blocked events
bob_blocked = db.query("SELECT * FROM blocked_events_ephemeral WHERE recorded_by = ?", (bob['peer_id'],))
print(f"Bob's blocked events: {len(bob_blocked)}")

# Check if Bob has the network key in key_ownership
print(f"\n=== key_ownership for network key AFTER bootstrap ===")
ownership_after = db.query("SELECT * FROM key_ownership WHERE key_id = ?", (alice['key_id'],))
print(f"Ownership entries: {len(ownership_after)}")
for o in ownership_after:
    print(f"  peer_id: {o['peer_id']}")

# Check if Bob's invite_keys_shared table has entries
print(f"\n=== invite_keys_shared table ===")
invite_keys_shared = db.query("SELECT * FROM invite_keys_shared")
print(f"Total entries: {len(invite_keys_shared)}")
for iks in invite_keys_shared:
    print(f"  event_id: {iks['invite_key_shared_id']}, recorded_by: {iks['recorded_by']}")
