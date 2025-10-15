#!/usr/bin/env python3
"""Minimal test to reproduce bloom filter false positive for channel."""
import sqlite3
from db import Database
import schema
from events.identity import user, invite, peer
from events.transit import sync
import crypto

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice and Bob
alice = user.new_network(name='Alice', t_ms=1000, db=db)
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

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

# Alice receives bootstrap
sync.receive(batch_size=20, t_ms=4100, db=db)
sync.receive(batch_size=20, t_ms=4150, db=db)

print("=== Testing Bloom Filter for Window 1 ===\n")

# Get Bob's shareable events in window 1
bob_window_1 = db.query(
    "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ? AND window_id >= 524288 AND window_id < 1048576",
    (bob['peer_id'],)
)
bob_event_ids = [row['event_id'] for row in bob_window_1]

print(f"Bob has {len(bob_event_ids)} events in window 1:")
for eid in bob_event_ids:
    print(f"  - {eid[:30]}...")

# Get Alice's shareable events in window 1
alice_window_1 = db.query(
    "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ? AND window_id >= 524288 AND window_id < 1048576",
    (alice['peer_id'],)
)
alice_event_ids = [row['event_id'] for row in alice_window_1]

print(f"\nAlice has {len(alice_event_ids)} events in window 1:")
for eid in alice_event_ids:
    is_channel = " ← CHANNEL" if eid == alice['channel_id'] else ""
    print(f"  - {eid[:30]}...{is_channel}")

# Build Bob's bloom filter (as Bob would when sending a sync request)
bob_event_bytes = [crypto.b64decode(eid) for eid in bob_event_ids]
bob_public_key = peer.get_public_key(bob['peer_id'], bob['peer_id'], db)
salt = sync.derive_salt(bob_public_key, window_id=1)
bob_bloom = sync.create_bloom(bob_event_bytes, salt)

bits_set = bin(int.from_bytes(bob_bloom, 'big')).count('1')
print(f"\nBob's bloom filter: {bits_set} bits set / 512 total")

# Test each of Alice's events against Bob's bloom
print(f"\n=== Testing Alice's Events Against Bob's Bloom ===\n")
false_positives = []
for eid in alice_event_ids:
    eid_bytes = crypto.b64decode(eid)
    in_bloom = sync.check_bloom(eid_bytes, bob_bloom, salt)

    is_channel = eid == alice['channel_id']
    should_be_in_bloom = eid in bob_event_ids

    status = "✓" if (in_bloom == should_be_in_bloom) else "❌"
    event_label = "CHANNEL" if is_channel else "event"

    print(f"{status} {event_label} {eid[:30]}...")
    print(f"   In Bob's events: {should_be_in_bloom}, In bloom: {in_bloom}")

    if in_bloom and not should_be_in_bloom:
        false_positives.append(eid)
        print(f"   ^^^ FALSE POSITIVE!")

# Summary
print(f"\n=== Summary ===")
print(f"Total false positives: {len(false_positives)}")
if false_positives:
    print(f"False positive rate: {len(false_positives) / len(alice_event_ids) * 100:.1f}%")

    for eid in false_positives:
        is_channel = eid == alice['channel_id']
        if is_channel:
            print(f"\n❌ CRITICAL: Channel is a false positive!")
            print(f"This is why Alice doesn't send the channel to Bob!")
