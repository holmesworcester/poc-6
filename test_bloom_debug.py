#!/usr/bin/env python3
"""Debug bloom filter to understand false positives."""
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

print("=== Event Analysis ===\n")
print(f"Alice channel ID: {alice['channel_id']}")
print(f"Bob channel ID: {bob['channel_id']}")
print(f"Same channel: {alice['channel_id'] == bob['channel_id']}\n")

# Get Bob's shareable events in window 1 (where Alice's channel should be)
bob_window_1_events = db.query(
    "SELECT event_id, window_id FROM shareable_events WHERE can_share_peer_id = ? AND window_id >= 524288 AND window_id < 1048576",
    (bob['peer_id'],)
)
print(f"Bob has {len(bob_window_1_events)} events in window 1:")
for evt in bob_window_1_events:
    print(f"  - {evt['event_id'][:20]}... (storage_window={evt['window_id']})")

# Get Alice's shareable events in window 1
alice_window_1_events = db.query(
    "SELECT event_id, window_id FROM shareable_events WHERE can_share_peer_id = ? AND window_id >= 524288 AND window_id < 1048576",
    (alice['peer_id'],)
)
print(f"\nAlice has {len(alice_window_1_events)} events in window 1:")
alice_channel_in_window_1 = False
for evt in alice_window_1_events:
    is_channel = " ← CHANNEL" if evt['event_id'] == alice['channel_id'] else ""
    print(f"  - {evt['event_id'][:20]}... (storage_window={evt['window_id']}){is_channel}")
    if evt['event_id'] == alice['channel_id']:
        alice_channel_in_window_1 = True

# Build Bob's bloom filter for window 1
print("\n=== Bloom Filter Creation ===\n")
bob_event_ids = [evt['event_id'] for evt in bob_window_1_events]
bob_event_bytes = [crypto.b64decode(eid) for eid in bob_event_ids]

bob_public_key = peer.get_public_key(bob['peer_id'], bob['peer_id'], db)
salt = sync.derive_salt(bob_public_key, window_id=1)

print(f"Creating bloom filter with {len(bob_event_bytes)} events")
print(f"Salt: {salt.hex()}")

bloom = sync.create_bloom(bob_event_bytes, salt)
bits_set = bin(int.from_bytes(bloom, 'big')).count('1')
print(f"Bloom filter: {bits_set} bits set out of {len(bloom) * 8}")

# Test Alice's channel against Bob's bloom
if alice_channel_in_window_1:
    print("\n=== Testing Alice's Channel ===\n")
    alice_channel_bytes = crypto.b64decode(alice['channel_id'])

    # Check if Alice's channel is in Bob's bloom
    in_bloom = sync.check_bloom(alice_channel_bytes, bloom, salt)

    print(f"Alice's channel: {alice['channel_id'][:30]}...")
    print(f"In Bob's bloom filter: {in_bloom}")

    if in_bloom:
        print("\n❌ FALSE POSITIVE! Alice's channel is NOT in Bob's events but bloom says it is!")
        print("This is why Alice isn't sending the channel to Bob.")

        # Calculate expected FPR
        k = 5  # number of hash functions
        m = 512  # bloom filter size in bits
        n = len(bob_event_bytes)  # number of elements
        import math
        expected_fpr = (1 - math.exp(-k * n / m)) ** k
        print(f"\nExpected FPR: {expected_fpr:.2%} (k={k}, m={m}, n={n})")
        print(f"With {n} elements, we expect ~{expected_fpr * 100:.1f}% false positive rate")
    else:
        print("\n✓ Alice's channel is NOT in bloom (correct!)")
        print("Alice should send this event to Bob.")

# Also test: Is Alice's channel actually IN Bob's event list?
print(f"\n=== Verification ===\n")
print(f"Is Alice's channel in Bob's shareable events? {alice['channel_id'] in bob_event_ids}")
