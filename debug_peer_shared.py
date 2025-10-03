"""Debug why Bob doesn't know Alice's peer_shared."""
import sqlite3
import schema
from db import Database
from events import user, sync

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates a network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id']}")
print(f"Alice peer_shared_id: {alice['peer_shared_id']}")

# Check if Alice's peer_shared is shareable
alice_peer_shared_shareable = db.query_one(
    "SELECT * FROM shareable_events WHERE event_id = ?",
    (alice['peer_shared_id'],)
)
print(f"\nAlice's peer_shared is shareable: {bool(alice_peer_shared_shareable)}")

# Bob joins
bob = user.join(invite_link=alice['invite_link'], name='Bob', t_ms=2000, db=db)
print(f"\nBob peer_id: {bob['peer_id']}")
print(f"Bob peer_shared_id: {bob['peer_shared_id']}")

# Bob sends bootstrap
print("\n=== Bob sends bootstrap ===")
user.send_bootstrap_events(
    peer_id=bob['peer_id'],
    peer_shared_id=bob['peer_shared_id'],
    user_id=bob['user_id'],
    prekey_shared_id=bob['prekey_shared_id'],
    invite_data=bob['invite_data'],
    t_ms=4000,
    db=db
)

# Check Bob's shareable events (should include his peer_shared sent to Alice's prekey)
bob_shareable = db.query("SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?", (bob['peer_id'],))
print(f"Bob has {len(bob_shareable)} shareable events")

# Alice receives Bob's bootstrap
print("\n=== Alice receives Bob's bootstrap (Round 1) ===")
alice_valid_before = db.query("SELECT event_id FROM valid_events WHERE recorded_by = ?", (alice['peer_id'],))
print(f"Alice valid events before: {len(alice_valid_before)}")

sync.receive(batch_size=20, t_ms=4100, db=db)

alice_valid_after = db.query("SELECT event_id FROM valid_events WHERE recorded_by = ?", (alice['peer_id'],))
print(f"Alice valid events after: {len(alice_valid_after)}")

# Does Alice know Bob's peer_shared?
bob_peer_shared_for_alice = db.query_one(
    "SELECT * FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
    (bob['peer_shared_id'], alice['peer_id'])
)
print(f"Alice knows Bob's peer_shared: {bool(bob_peer_shared_for_alice)}")

# Round 2 - process unblocked events
print("\n=== Alice processes unblocked events (Round 2) ===")
sync.receive(batch_size=20, t_ms=4150, db=db)

alice_valid_after2 = db.query("SELECT event_id FROM valid_events WHERE recorded_by = ?", (alice['peer_id'],))
print(f"Alice valid events: {len(alice_valid_after2)}")

bob_peer_shared_for_alice2 = db.query_one(
    "SELECT * FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
    (bob['peer_shared_id'], alice['peer_id'])
)
print(f"Alice knows Bob's peer_shared: {bool(bob_peer_shared_for_alice2)}")

# Round 3 - Alice sends sync response
print("\n=== Alice sends sync response (Round 3) ===")
sync.receive(batch_size=20, t_ms=4200, db=db)

# Bob receives Alice's sync response
print("\n=== Bob receives Alice's sync response (Round 4) ===")
bob_valid_before = db.query("SELECT event_id FROM valid_events WHERE recorded_by = ?", (bob['peer_id'],))
print(f"Bob valid events before: {len(bob_valid_before)}")

sync.receive(batch_size=20, t_ms=4300, db=db)

bob_valid_after = db.query("SELECT event_id FROM valid_events WHERE recorded_by = ?", (bob['peer_id'],))
print(f"Bob valid events after: {len(bob_valid_after)}")

# Does Bob know Alice's peer_shared?
alice_peer_shared_for_bob = db.query_one(
    "SELECT * FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
    (alice['peer_shared_id'], bob['peer_id'])
)
print(f"Bob knows Alice's peer_shared: {bool(alice_peer_shared_for_bob)}")

# Check if Bob received Alice's peer_shared event at all
print(f"\n=== Checking if Bob has Alice's peer_shared event ===")
import crypto
alice_peer_shared_in_store = db.query_one(
    "SELECT * FROM store WHERE id = ?",
    (crypto.b64decode(alice['peer_shared_id']),)  # Store uses bytes, not base64 string
)
print(f"Alice's peer_shared exists in store: {bool(alice_peer_shared_in_store)}")

# Check if it's shareable for Alice
alice_peer_shared_shareable_check = db.query_one(
    "SELECT * FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
    (alice['peer_shared_id'], alice['peer_id'])
)
print(f"Alice's peer_shared is shareable by Alice: {bool(alice_peer_shared_shareable_check)}")

# Check if it's valid for Bob
alice_peer_shared_valid_for_bob = db.query_one(
    "SELECT * FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (alice['peer_shared_id'], bob['peer_id'])
)
print(f"Alice's peer_shared is valid for Bob: {bool(alice_peer_shared_valid_for_bob)}")

# Now check key_ownership - does Bob have Alice's network key?
print(f"\n=== Checking key_ownership ===")
ownership = db.query("SELECT * FROM key_ownership WHERE key_id = ?", (alice['key_id'],))
print(f"Total ownership entries for Alice's network key: {len(ownership)}")
for o in ownership:
    peer_name = "Alice" if o['peer_id'] == alice['peer_id'] else "Bob" if o['peer_id'] == bob['peer_id'] else "Unknown"
    print(f"  {peer_name}: {o['peer_id']}")

# Check invite_keys_shared table
invite_keys_shared = db.query("SELECT * FROM invite_keys_shared")
print(f"\ninvite_keys_shared table entries: {len(invite_keys_shared)}")

# Need additional bloom sync rounds like the test
print("\n=== Additional bloom sync rounds ===")
sync.sync_all(t_ms=4400, db=db)
sync.receive(batch_size=20, t_ms=4500, db=db)
sync.receive(batch_size=20, t_ms=4600, db=db)
sync.sync_all(t_ms=4700, db=db)
sync.receive(batch_size=20, t_ms=4800, db=db)
sync.receive(batch_size=20, t_ms=4900, db=db)

db.commit()

# Check again
print("\n=== Final check after bloom sync ===")
bob_knows_alice_final = db.query_one("SELECT * FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ?",
                                      (alice['peer_shared_id'], bob['peer_id']))
print(f"Bob knows Alice's peer_shared: {bool(bob_knows_alice_final)}")

ownership_final = db.query("SELECT * FROM key_ownership WHERE key_id = ?", (alice['key_id'],))
print(f"Total ownership entries: {len(ownership_final)}")
for o in ownership_final:
    peer_name = "Alice" if o['peer_id'] == alice['peer_id'] else "Bob" if o['peer_id'] == bob['peer_id'] else "Unknown"
    print(f"  {peer_name}: {o['peer_id']}")

invite_keys_shared_final = db.query("SELECT * FROM invite_keys_shared")
print(f"\ninvite_keys_shared entries: {len(invite_keys_shared_final)}")

# Check Bob's blocked events
bob_blocked_final = db.query("SELECT * FROM blocked_events_ephemeral WHERE recorded_by = ?", (bob['peer_id'],))
print(f"\nBob's blocked events: {len(bob_blocked_final)}")
for b in bob_blocked_final[:3]:  # Show first 3
    print(f"  recorded_id: {b['recorded_id']}, missing_deps: {b['missing_deps']}")

# Parse the missing deps to check if Alice's peer_shared is the blocker
if bob_blocked_final:
    import json
    first_blocked = bob_blocked_final[0]
    missing_deps = json.loads(first_blocked['missing_deps'])
    print(f"\nFirst blocked event's missing deps: {missing_deps}")
    print(f"Alice's peer_shared_id: {alice['peer_shared_id']}")
    if alice['peer_shared_id'] in missing_deps:
        print("  ✗ Alice's peer_shared is blocking Bob!")
        # Check if Alice's peer_shared is valid for Bob
        is_valid = db.query_one("SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
                                (alice['peer_shared_id'], bob['peer_id']))
        print(f"  Alice's peer_shared is valid for Bob: {bool(is_valid)}")

        # Check if Alice's peer_shared blob is in store
        import crypto
        alice_ps_in_store = db.query_one("SELECT * FROM store WHERE id = ?",
                                          (crypto.b64decode(alice['peer_shared_id']),))
        print(f"  Alice's peer_shared in store: {bool(alice_ps_in_store)}")

        # Check if there's a blocked recorded event for Alice's peer_shared for Bob
        alice_ps_blocked_for_bob = db.query("SELECT * FROM blocked_events_ephemeral WHERE recorded_by = ?", (bob['peer_id'],))
        for b in alice_ps_blocked_for_bob:
            recorded_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (crypto.b64decode(b['recorded_id']),))
            if recorded_blob:
                recorded_event = crypto.parse_json(recorded_blob['blob'])
                if recorded_event.get('ref_id') == alice['peer_shared_id']:
                    print(f"  Alice's peer_shared recorded event is BLOCKED for Bob!")
                    print(f"    missing_deps: {b['missing_deps']}")
