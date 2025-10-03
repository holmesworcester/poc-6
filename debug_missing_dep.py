"""Debug what event ID LQalDfXhmZp6GTej80EYvQ== is."""
import sqlite3
import schema
from db import Database
from events import user, sync, message

# Setup: Initialize in-memory database
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates a network (implicit network via first group)
alice = user.new_network(name='Alice', t_ms=1000, db=db)

# Bob joins Alice's network via invite
bob = user.join(invite_link=alice['invite_link'], name='Bob', t_ms=2000, db=db)

# Bootstrap: Fully realistic protocol
user.send_bootstrap_events(
    peer_id=bob['peer_id'],
    peer_shared_id=bob['peer_shared_id'],
    user_id=bob['user_id'],
    prekey_shared_id=bob['prekey_shared_id'],
    invite_data=bob['invite_data'],
    t_ms=4000,
    db=db
)

# Alice receives Bob's bootstrap events
sync.receive(batch_size=20, t_ms=4100, db=db)
sync.receive(batch_size=20, t_ms=4150, db=db)
sync.receive(batch_size=20, t_ms=4200, db=db)

# Bob receives Alice's sync response
sync.receive(batch_size=20, t_ms=4300, db=db)

# Continue with bloom sync
sync.sync_all(t_ms=4400, db=db)
sync.receive(batch_size=20, t_ms=4500, db=db)
sync.receive(batch_size=20, t_ms=4600, db=db)
sync.sync_all(t_ms=4700, db=db)
sync.receive(batch_size=20, t_ms=4800, db=db)
sync.receive(batch_size=20, t_ms=4900, db=db)

db.commit()

# Alice creates a message
print("\n=== Creating Alice's message ===")
alice_msg = message.create_message(
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
print(f"Alice message ID: {alice_msg}")

# Bob creates a message
print("\n=== Creating Bob's message ===")
bob_msg = message.create_message(
    params={
        'content': 'Hello from Bob',
        'channel_id': alice['channel_id'],
        'group_id': alice['group_id'],
        'peer_id': bob['peer_id'],
        'peer_shared_id': bob['peer_shared_id'],
        'key_id': alice['key_id']
    },
    t_ms=6000,
    db=db
)
print(f"Bob message ID: {bob_msg}")

# Check if Bob has the network key
print("\n=== Checking Bob's keys ===")
bob_keys = db.query("SELECT * FROM keys")
print(f"Bob has {len(bob_keys)} symmetric keys")
for k in bob_keys:
    print(f"  key_id: {k['key_id']}")

# Check key_ownership
print("\n=== Checking key_ownership ===")
key_ownerships = db.query("SELECT * FROM key_ownership")
print(f"Total key_ownership entries: {len(key_ownerships)}")
for ko in key_ownerships:
    print(f"  key_id: {ko['key_id']}, peer_id: {ko['peer_id']}")

# Check if Bob has Alice's network key
print(f"\n=== Does Bob have Alice's network key ({alice['key_id']})? ===")
bob_has_key = db.query_one("SELECT * FROM key_ownership WHERE key_id = ? AND peer_id = ?",
                            (alice['key_id'], bob['peer_id']))
print(f"Bob has key ownership: {bool(bob_has_key)}")

# Check invite_keys_shared table
print(f"\n=== Checking invite_keys_shared table ===")
invite_keys_shared = db.query("SELECT * FROM invite_keys_shared")
print(f"Total invite_keys_shared entries: {len(invite_keys_shared)}")
for iks in invite_keys_shared:
    print(f"  event_id: {iks['invite_key_shared_id']}")
    print(f"    original_key_id: {iks['original_key_id']}")
    print(f"    recorded_by: {iks['recorded_by']}")
    print(f"    created_by: {iks['created_by']}")

# Check if invite_key_shared was created
print(f"\n=== Looking for invite_key_shared event in store ===")
invite_key_shared_id = bob['invite_data'].get('invite_key_shared_id')
print(f"Expected invite_key_shared_id from Bob's invite: {invite_key_shared_id}")
# Also check Alice's invite_link data
invite_link_data = alice.get('invite_link_data')
if invite_link_data:
    alice_iks_id = invite_link_data.get('invite_key_shared_id')
    print(f"Expected invite_key_shared_id from Alice's invite_link: {alice_iks_id}")
    invite_key_shared_id = alice_iks_id or invite_key_shared_id
if invite_key_shared_id:
    iks_blob = db.query_one("SELECT * FROM store WHERE id = ?", (invite_key_shared_id,))
    if iks_blob:
        print(f"  Found in store, blob length: {len(iks_blob['blob'])}")
        # Check if Bob can decrypt it
        plaintext, missing = crypto.unwrap(iks_blob['blob'], bob['peer_id'], db)
        if plaintext:
            print(f"  Bob CAN decrypt it!")
            data = crypto.parse_json(plaintext)
            print(f"  Key being shared: {data.get('key_id')}")
        else:
            print(f"  Bob CANNOT decrypt it, missing keys: {missing}")
    else:
        print(f"  NOT found in store")

# Check if it was marked as shareable for Alice
if invite_key_shared_id:
    shareable = db.query("SELECT * FROM shareable_events WHERE event_id = ?", (invite_key_shared_id,))
    print(f"\n=== Shareable entries for invite_key_shared: {len(shareable)} ===")
    for s in shareable:
        print(f"  recorded_by_peer_id: {s['recorded_by_peer_id']}")

# Check ALL recorded events to see if ANY are of type invite_key_shared
print(f"\n=== Looking for ALL recorded events of type invite_key_shared ===")
all_recorded = db.query("SELECT * FROM recorded")
print(f"Total recorded events: {len(all_recorded)}")
for r in all_recorded:
    blob = db.query_one("SELECT * FROM store WHERE id = ?", (r['event_id'],))
    if blob:
        try:
            # Try to unwrap/parse to get type
            plaintext, _ = crypto.unwrap(blob['blob'], r['recorded_by'], db)
            if plaintext:
                data = crypto.parse_json(plaintext)
                if data.get('type') == 'invite_key_shared':
                    print(f"  FOUND: event_id={r['event_id']}, recorded_by={r['recorded_by']}")
        except:
            pass

# Round 1: Send sync requests
print("\n=== Round 1: Sync requests ===")
sync.sync_all(t_ms=8000, db=db)
sync.receive(batch_size=10, t_ms=9000, db=db)
sync.receive(batch_size=100, t_ms=10000, db=db)

# Check what event LQalDfXhmZp6GTej80EYvQ== is
import crypto
event_id = 'LQalDfXhmZp6GTej80EYvQ=='

# Look in store
blob = db.query_one("SELECT * FROM store WHERE id = ?", (event_id,))
if blob:
    print(f"\n=== Event {event_id} found in store ===")
    print(f"Blob length: {len(blob['blob'])}")
    # Try to parse it
    try:
        data = crypto.parse_json(blob['blob'])
        print(f"Type: {data.get('type')}")
        print(f"Data: {data}")
    except:
        print("Cannot parse as JSON (might be wrapped)")
else:
    print(f"\n=== Event {event_id} NOT found in store ===")

# Check all Alice's events
print("\n=== Alice's events in store ===")
alice_events = db.query("SELECT id FROM store")
for e in alice_events:
    print(f"  {e['id']}")

# Check if it's in shareable_events
shareable = db.query("SELECT * FROM shareable_events WHERE event_id = ?", (event_id,))
print(f"\n=== Shareable entries for {event_id}: {len(shareable)} ===")
for s in shareable:
    print(f"  recorded_by={s['recorded_by']}")

# Check valid_events
valid = db.query("SELECT * FROM valid_events WHERE event_id = ?", (event_id,))
print(f"\n=== Valid entries for {event_id}: {len(valid)} ===")
for v in valid:
    print(f"  recorded_by={v['recorded_by']}")

# Check what Alice and Bob know about each other
print(f"\n=== Alice's peer_id: {alice['peer_id']} ===")
print(f"=== Bob's peer_id: {bob['peer_id']} ===")

# Check blocked queue
blocked = db.query("SELECT * FROM blocked_events_ephemeral")
print(f"\n=== Blocked events: {len(blocked)} ===")
for b in blocked[:10]:  # Show first 10
    print(f"  recorded_id={b['recorded_id']}, peer={b['recorded_by']}, missing_deps={b['missing_deps']}")

# Check what events have this as a dependency
import json
print(f"\n=== Events that depend on {event_id} ===")
for b in blocked:
    try:
        deps = json.loads(b['missing_deps'])
        if event_id in deps:
            print(f"  {b['recorded_id']} (by {b['recorded_by']}) is waiting for it")
            # Check if this event exists in store
            evt_blob = db.query_one("SELECT * FROM store WHERE id = ?", (b['recorded_id'],))
            if evt_blob:
                print(f"    Event exists in store (blob length: {len(evt_blob['blob'])})")
                # Check its deps
                try:
                    evt_data = crypto.parse_json(evt_blob['blob'])
                    if 'deps' in evt_data:
                        print(f"    deps field: {evt_data['deps']}")
                except:
                    pass
    except:
        pass
