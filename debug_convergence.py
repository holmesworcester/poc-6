"""
Debug script to replay convergence failure and understand the duplicate channel issue.
"""
import sqlite3
import json
from db import Database
import schema
from events.identity import user, invite
from events.content import message
from events.transit import sync
from tests.utils.convergence import _get_projectable_event_ids, _recreate_projection_tables, _replay_events, _dump_projection_state

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Create test scenario
alice = user.new_network(name='Alice', t_ms=1000, db=db)
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
bob = user.join(invite_link=invite_link, name='Bob', t_ms=2000, db=db)
charlie = user.new_network(name='Charlie', t_ms=3000, db=db)

db.commit()

# Initial sync
sync.send_request_to_all(t_ms=4000, db=db)
db.commit()
sync.receive(batch_size=20, t_ms=4100, db=db)
db.commit()

sync.send_request_to_all(t_ms=4200, db=db)
db.commit()
sync.receive(batch_size=20, t_ms=4300, db=db)
db.commit()

# Create messages
message.create(peer_id=alice['peer_id'], channel_id=alice['channel_id'], content="Hello from Alice!", t_ms=5000, db=db)
db.commit()
message.create(peer_id=bob['peer_id'], channel_id=bob['channel_id'], content="Hello from Bob!", t_ms=5100, db=db)
db.commit()
message.create(peer_id=charlie['peer_id'], channel_id=charlie['channel_id'], content="Hello from Charlie!", t_ms=5200, db=db)
db.commit()

for round_num in range(3):
    sync.send_request_to_all(t_ms=6000 + round_num * 100, db=db)
    db.commit()
    sync.receive(batch_size=20, t_ms=6050 + round_num * 100, db=db)
    db.commit()

# Get event IDs and baseline state
event_ids = _get_projectable_event_ids(db)
baseline_state = _dump_projection_state(db)

print("=== Baseline Channels ===")
for row in baseline_state.get('channels', []):
    print(f"  {row}")

print(f"\n=== Searching for duplicate channel event ===")
# Find all events that might be channels by looking at the recorded events
channel_recorded = db.query("""
    SELECT event_id, recorded_by, recorded_at, event_type
    FROM recorded
    WHERE event_type = 'channel'
    ORDER BY recorded_at
""")

print(f"Found {len(channel_recorded)} channel recorded events:")
for row in channel_recorded:
    print(f"  {row}")

# Check if there are multiple projection events for the same channel
print(f"\n=== Checking channels table directly ===")
channels = db.query("SELECT * FROM channels ORDER BY recorded_at")
print(f"Found {len(channels)} channel rows:")
for ch in channels:
    print(f"  channel_id={ch['channel_id'][:20]}..., recorded_by={ch['recorded_by'][:20]}..., recorded_at={ch['recorded_at']}")
