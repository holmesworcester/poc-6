#!/usr/bin/env python3
"""Find which window Alice's channel is in."""
import sqlite3
from db import Database
import schema
from events.identity import user, invite
from events.transit import sync
import crypto

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create Alice
alice = user.new_network(name='Alice', t_ms=1000, db=db)

print(f"Alice channel ID: {alice['channel_id']}\n")

# Check shareable_events for Alice's channel
channel_rows = db.query(
    "SELECT event_id, can_share_peer_id, window_id FROM shareable_events WHERE event_id = ?",
    (alice['channel_id'],)
)

if channel_rows:
    for row in channel_rows:
        storage_window = row['window_id']
        channel_bytes = crypto.b64decode(alice['channel_id'])

        # Calculate sync window ID for different w values
        print(f"Channel in shareable_events:")
        print(f"  Storage window ID: {storage_window}")
        print(f"  Can share peer: {row['can_share_peer_id'][:20]}...\n")

        for w in [1, 2, 3, 4]:
            sync_window = storage_window >> (20 - w)  # STORAGE_W = 20
            window_min = sync_window << (20 - w)
            window_max = (sync_window + 1) << (20 - w)
            print(f"  With w={w}: sync_window={sync_window}, range=[{window_min}, {window_max})")
else:
    print(f"❌ Channel NOT in shareable_events!")

    # Check if it's in valid_events
    valid_rows = db.query(
        "SELECT event_id, recorded_by FROM valid_events WHERE event_id = ?",
        (alice['channel_id'],)
    )
    if valid_rows:
        print(f"✓ Channel IS in valid_events:")
        for row in valid_rows:
            print(f"  recorded_by: {row['recorded_by'][:20]}...")
    else:
        print(f"❌ Channel NOT in valid_events either!")

# Check all Alice's shareable events
print(f"\n=== All Alice's shareable events ===")
all_alice_events = db.query(
    "SELECT event_id, window_id FROM shareable_events WHERE can_share_peer_id = ? ORDER BY window_id",
    (alice['peer_id'],)
)
print(f"Total: {len(all_alice_events)} events\n")
for evt in all_alice_events:
    is_channel = " ← CHANNEL" if evt['event_id'] == alice['channel_id'] else ""
    print(f"  {evt['event_id'][:30]}... window={evt['window_id']}{is_channel}")
