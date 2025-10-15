#!/usr/bin/env python3
"""Check if Bob has Alice's channel in his shareable_events after joining."""
import sqlite3
from db import Database
import schema
from events.identity import user, invite
from events.transit import sync

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice and Bob
alice = user.new_network(name='Alice', t_ms=1000, db=db)
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)

print(f"Alice peer_id: {alice['peer_id'][:20]}...")
print(f"Bob peer_id: {bob['peer_id'][:20]}...")
print(f"Alice channel: {alice['channel_id']}")
print(f"Bob channel: {bob['channel_id']}")
print(f"Same channel: {alice['channel_id'] == bob['channel_id']}\n")

# Check Bob's shareable events
bob_shareable = db.query(
    "SELECT event_id, window_id FROM shareable_events WHERE can_share_peer_id = ?",
    (bob['peer_id'],)
)

print(f"Bob has {len(bob_shareable)} shareable events:")
for evt in bob_shareable:
    is_channel = " ← CHANNEL" if evt['event_id'] == bob['channel_id'] else ""
    print(f"  {evt['event_id'][:30]}... window={evt['window_id']}{is_channel}")

# Check if channel is in Bob's shareable events
channel_in_bob_shareable = any(evt['event_id'] == bob['channel_id'] for evt in bob_shareable)
print(f"\nChannel in Bob's shareable events: {channel_in_bob_shareable}")

# Check Alice's shareable events
alice_shareable = db.query(
    "SELECT event_id, window_id FROM shareable_events WHERE can_share_peer_id = ?",
    (alice['peer_id'],)
)

print(f"\nAlice has {len(alice_shareable)} shareable events:")
for evt in alice_shareable:
    is_channel = " ← CHANNEL" if evt['event_id'] == alice['channel_id'] else ""
    print(f"  {evt['event_id'][:30]}... window={evt['window_id']}{is_channel}")

# Check if channel is in Alice's shareable events
channel_in_alice_shareable = any(evt['event_id'] == alice['channel_id'] for evt in alice_shareable)
print(f"\nChannel in Alice's shareable events: {channel_in_alice_shareable}")

# Find events in common
bob_ids = set(evt['event_id'] for evt in bob_shareable)
alice_ids = set(evt['event_id'] for evt in alice_shareable)
common = bob_ids & alice_ids

print(f"\n=== Events in common ===")
print(f"Total: {len(common)} events")
for event_id in common:
    is_channel = " ← CHANNEL" if event_id == bob['channel_id'] else ""
    print(f"  {event_id[:30]}...{is_channel}")

# The key insight: After joining, does Bob have the channel in his shareable events?
if bob['channel_id'] in bob_ids:
    print(f"\n✓ Bob has the channel in shareable_events (expected)")
else:
    print(f"\n❌ Bob does NOT have the channel in shareable_events!")
    print(f"This means Bob cannot include it in his bloom filter!")
