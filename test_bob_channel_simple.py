#!/usr/bin/env python3
"""Simple test to check if Bob receives and validates Alice's channel."""
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

print(f"Alice channel: {alice['channel_id']}")
print(f"Bob channel: {bob['channel_id']}")
print(f"Same: {alice['channel_id'] == bob['channel_id']}\n")

# Sync multiple rounds
for round_num in range(10):
    t_base = 4100 + (round_num * 100)

    sync.receive(batch_size=20, t_ms=t_base, db=db)
    sync.send_request_to_all(t_ms=t_base + 50, db=db)

    # Check if Bob has channel in valid_events
    bob_has_channel = db.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (bob['channel_id'], bob['peer_id'])
    )

    # Check if Bob has channel in channels table
    bob_channel_projected = db.query_one(
        "SELECT 1 FROM channels WHERE channel_id = ? AND recorded_by = ?",
        (bob['channel_id'], bob['peer_id'])
    )

    print(f"Round {round_num + 1}: valid={bool(bob_has_channel)}, projected={bool(bob_channel_projected)}")

    if bob_has_channel and bob_channel_projected:
        print(f"\n✓ SUCCESS! Bob has channel after {round_num + 1} sync rounds")
        break
else:
    print(f"\n❌ FAILURE: Bob doesn't have channel after 10 sync rounds")

    # Debug: Check blocked events
    blocked = db.query("SELECT COUNT(*) as cnt FROM blocked_events WHERE recorded_by = ?", (bob['peer_id'],))
    print(f"Bob has {blocked[0]['cnt']} blocked events")

    # Debug: Check shareable_events
    shareable = db.query("SELECT COUNT(*) as cnt FROM shareable_events WHERE can_share_peer_id = ? AND event_id = ?", (bob['peer_id'], bob['channel_id']))
    print(f"Channel in Bob's shareable_events: {shareable[0]['cnt'] > 0}")
