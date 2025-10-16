#!/usr/bin/env python3
"""Debug GKS blocking issue"""
import sqlite3
from db import Database
import schema
from events.identity import user
from events.transit import sync

# Create test database
conn = sqlite3.Connection(':memory:')
db = Database(conn)
schema.create_all(db)

# Create Alice
from events.identity import peer
alice_peer_id = peer.create(t_ms=1000, db=db)
alice_peer_shared_id = peer.create_peer_shared(alice_peer_id, t_ms=1000, db=db)
alice = user.new_network(alice_peer_id, alice_peer_shared_id, 'Network', 'Main', t_ms=1000, db=db)
db.commit()

# Create invite
from events.identity import invite
invite_id, invite_link, invite_data = invite.create(alice_peer_id, t_ms=1500, db=db)
db.commit()

print(f"\nAlice created invite:")
print(f"  invite_id: {invite_id[:20]}...")
print(f"  invite_prekey_id: {invite_data['invite_prekey_id'][:20]}...")

# Check GKS event in shareable_events
gks_events = db.query("""
    SELECT se.event_id, se.can_share_peer_id
    FROM shareable_events se
    JOIN store s ON s.id = se.event_id
    WHERE s.blob LIKE '%group_key_shared%'
""")
print(f"\nGKS events in shareable_events: {len(gks_events)}")
for row in gks_events:
    print(f"  event_id: {row['event_id'][:20]}..., can_share: {row['can_share_peer_id'][:20]}...")

# Bob joins
bob_peer_id = peer.create(t_ms=2000, db=db)
bob_peer_shared_id = peer.create_peer_shared(bob_peer_id, t_ms=2000, db=db)
bob_user_id = user.join(bob_peer_id, bob_peer_shared_id, 'Bob', invite_data, t_ms=2000, db=db)
db.commit()

print(f"\nBob joined:")
print(f"  peer_id: {bob_peer_id[:20]}...")
print(f"  peer_shared_id: {bob_peer_shared_id[:20]}...")

# Check Bob's group_prekeys
bob_prekeys = db.query("""
    SELECT prekey_id, owner_peer_id
    FROM group_prekeys
    WHERE recorded_by = ?
""", (bob_peer_id,))
print(f"\nBob's group_prekeys: {len(bob_prekeys)}")
for row in bob_prekeys:
    print(f"  prekey_id: {row['prekey_id'][:20]}..., owner: {row['owner_peer_id'][:20]}...")

# Run sync
print(f"\n=== Running sync ===")
sync.send_request_to_all(t_ms=3000, db=db)
sync.receive(batch_size=20, t_ms=3100, db=db)
db.commit()

# Check blocked events
blocked = db.query("""
    SELECT recorded_id, recorded_by, missing_deps
    FROM blocked_events_ephemeral
""")
print(f"\nBlocked events: {len(blocked)}")
for row in blocked:
    print(f"  recorded_id: {row['recorded_id'][:20]}...")
    print(f"  recorded_by: {row['recorded_by'][:20]}...")
    print(f"  missing_deps: {row['missing_deps']}")

    # Try to identify what event is blocked
    import crypto
    rec_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (row['recorded_id'],))
    if rec_blob:
        rec_data = crypto.parse_json(rec_blob['blob'])
        ref_id = rec_data.get('ref_id')
        if ref_id:
            ref_blob = db.query_one("SELECT blob FROM store WHERE id = ?", (ref_id,))
            if ref_blob:
                try:
                    ref_data = crypto.parse_json(ref_blob['blob'])
                    print(f"  blocked event type: {ref_data.get('type')}")
                except:
                    # Try to unwrap
                    plaintext, missing = crypto.unwrap_event(ref_blob['blob'], row['recorded_by'], db)
                    if plaintext:
                        ref_data = crypto.parse_json(plaintext)
                        print(f"  blocked event type (after unwrap): {ref_data.get('type')}")
                    else:
                        print(f"  blocked event: encrypted, missing keys: {missing}")

# Check Bob's group_keys
bob_keys = db.query("""
    SELECT key_id
    FROM group_keys
    WHERE recorded_by = ?
""", (bob_peer_id,))
print(f"\nBob's group_keys: {len(bob_keys)}")
for row in bob_keys:
    print(f"  key_id: {row['key_id'][:20]}...")
