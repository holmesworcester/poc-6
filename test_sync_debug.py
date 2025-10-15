#!/usr/bin/env python3
"""Debug script to investigate why Bob doesn't receive Alice's channel."""
import sqlite3
from db import Database
import schema
from events.transit import sync
from events.identity import user, invite

# Setup: Initialize in-memory database
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates a network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"Alice peer_id: {alice['peer_id'][:20]}...")
print(f"Alice channel_id: {alice['channel_id'][:20]}...")

# Check Alice's shareable_events
alice_shareable = db.query(
    "SELECT event_id, window_id FROM shareable_events WHERE can_share_peer_id = ?",
    (alice['peer_id'],)
)
print(f"\nAlice has {len(alice_shareable)} shareable events:")
channel_in_shareable = False
for evt in alice_shareable:
    if evt['event_id'] == alice['channel_id']:
        print(f"  ✓ CHANNEL: {evt['event_id'][:20]}... in window {evt['window_id']}")
        channel_in_shareable = True
    else:
        # Get event type
        blob_row = db.query_one("SELECT blob FROM store WHERE id = ?", (evt['event_id'],))
        if blob_row:
            import crypto
            try:
                data = crypto.parse_json(blob_row['blob'])
                print(f"  - {data.get('type', 'unknown')}: {evt['event_id'][:20]}... in window {evt['window_id']}")
            except:
                print(f"  - (unparseable): {evt['event_id'][:20]}... in window {evt['window_id']}")

if not channel_in_shareable:
    print(f"\n❌ ERROR: Alice's channel {alice['channel_id'][:20]}... is NOT in shareable_events!")
else:
    print(f"\n✓ Alice's channel is in shareable_events")

# Create invite
invite_id, invite_link, invite_data = invite.create(
    peer_id=alice['peer_id'],
    t_ms=1500,
    db=db
)

# Bob joins
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"\nBob peer_id: {bob['peer_id'][:20]}...")
print(f"Bob channel_id: {bob['channel_id'][:20]}...")
print(f"Are they the same channel? {alice['channel_id'] == bob['channel_id']}")

# Send bootstrap
user.send_bootstrap_events(
    peer_id=bob['peer_id'],
    peer_shared_id=bob['peer_shared_id'],
    user_id=bob['user_id'],
    transit_prekey_shared_id=bob['transit_prekey_shared_id'],
    invite_data=bob['invite_data'],
    t_ms=4000,
    db=db
)

# Alice receives Bob's bootstrap
sync.receive(batch_size=20, t_ms=4100, db=db)
sync.receive(batch_size=20, t_ms=4150, db=db)
sync.receive(batch_size=20, t_ms=4200, db=db)

# Bob receives Alice's response
sync.receive(batch_size=20, t_ms=4300, db=db)

# Check if Bob has the channel
bob_has_channel = db.query_one(
    "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
    (bob['channel_id'], bob['peer_id'])
)

print(f"\nBob has channel after first sync: {bool(bob_has_channel)}")

# Enable detailed sync logging
import logging
sync_logger = logging.getLogger('events.transit.sync')
sync_logger.setLevel(logging.WARNING)

# Do more sync rounds
for i in range(10):
    t = 5000 + (i * 100)
    print(f"\n=== Sync round {i+1} ===")
    sync.send_request_to_all(t_ms=t, db=db)
    sync.receive(batch_size=20, t_ms=t + 50, db=db)

    bob_has_channel = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob['channel_id'], bob['peer_id'])
    )
    if bob_has_channel:
        print(f"✓ Bob received channel after sync round {i+1}")
        break

if not bob_has_channel:
    print(f"❌ Bob still doesn't have channel after 10 sync rounds")

    # Check Bob's shareable events
    bob_shareable = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    )
    print(f"\nBob has {len(bob_shareable)} shareable events:")
    for evt in bob_shareable:
        print(f"  - {evt['event_id'][:20]}...")

    # Check Alice's shareable events
    alice_shareable = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    )
    print(f"\nAlice has {len(alice_shareable)} shareable events:")
    for evt in alice_shareable:
        is_channel = " (CHANNEL)" if evt['event_id'] == alice['channel_id'] else ""
        print(f"  - {evt['event_id'][:20]}...{is_channel}")

    # Check if channel is in Bob's shareable_events
    channel_in_shareable = db.query_one(
        "SELECT 1 FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (bob['channel_id'], bob['peer_id'])
    )
    print(f"Channel in Bob's shareable_events: {bool(channel_in_shareable)}")

    # Check if channel is in Bob's valid_events
    channel_in_valid = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob['channel_id'], bob['peer_id'])
    )
    print(f"Channel in Bob's valid_events: {bool(channel_in_valid)}")

    # Debug: Check Bob's bloom filter
    # Manually build a bloom filter for Bob's window 0 shareable events
    import crypto
    from events.transit.sync import create_bloom, check_bloom, derive_salt
    from events.identity import peer as peer_module

    # Get window ID for Alice's channel
    alice_channel_window_row = db.query_one(
        "SELECT window_id FROM shareable_events WHERE event_id = ? AND can_share_peer_id = ?",
        (alice['channel_id'], alice['peer_id'])
    )
    alice_channel_window = alice_channel_window_row['window_id'] if alice_channel_window_row else None
    print(f"\nAlice's channel is in storage window: {alice_channel_window}")

    # Calculate which sync window this maps to (with w=1)
    if alice_channel_window:
        sync_window_for_channel = alice_channel_window >> (20 - 1)  # STORAGE_W - w_param
        print(f"This maps to sync window: {sync_window_for_channel}")

    # Check bloom filter for WINDOW 1 (where Alice's channel should be)
    bob_window_1_events = [evt['event_id'] for evt in bob_shareable if db.query_one(
        "SELECT 1 FROM shareable_events WHERE event_id = ? AND window_id >= 524288 AND window_id < 1048576",
        (evt['event_id'],)
    )]
    bob_public_key = peer_module.get_public_key(bob['peer_id'], bob['peer_id'], db)
    salt_window_1 = derive_salt(bob_public_key, 1)

    event_id_bytes_list = [crypto.b64decode(eid) for eid in bob_window_1_events]
    bob_bloom_w1 = create_bloom(event_id_bytes_list, salt_window_1)

    print(f"\n=== Bloom Filter Debug (Window 1) ===")
    print(f"Bob has {len(bob_window_1_events)} events in window 1 (524288-1048575)")
    print(f"Bob's bloom filter: {bob_bloom_w1.hex()[:40]}... (first 20 bytes)")
    print(f"Bloom filter has {bin(int.from_bytes(bob_bloom_w1, 'big')).count('1')} bits set out of {len(bob_bloom_w1)*8}")

    # Test: Does Alice's channel match Bob's bloom filter for window 1?
    alice_channel_bytes = crypto.b64decode(alice['channel_id'])
    channel_in_bloom_w1 = check_bloom(alice_channel_bytes, bob_bloom_w1, salt_window_1)
    print(f"\nAlice's channel in Bob's bloom filter (window 1): {channel_in_bloom_w1}")
    print(f"(This should be False since Bob doesn't have the channel)")

    # Check if Alice and Bob are using the same public key
    from events.identity import peer_shared as peer_shared_module
    bob_pk_from_peer = peer_module.get_public_key(bob['peer_id'], bob['peer_id'], db)
    bob_pk_from_peer_shared = peer_shared_module.get_public_key(bob['peer_shared_id'], alice['peer_id'], db)

    print(f"\n=== Public Key Mismatch Check ===")
    print(f"Bob's public key (from peer table): {bob_pk_from_peer.hex()[:40]}...")
    print(f"Bob's public key (from peer_shared, Alice's view): {bob_pk_from_peer_shared.hex()[:40]}...")
    print(f"Keys match: {bob_pk_from_peer == bob_pk_from_peer_shared}")

    # Check incoming_blobs
    incoming = db.query("SELECT COUNT(*) as cnt FROM incoming_blobs")
    print(f"\nincoming_blobs: {incoming[0]['cnt']} blobs")
