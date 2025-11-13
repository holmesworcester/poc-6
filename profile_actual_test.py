#!/usr/bin/env python3
"""Profile the actual test_large_file_sync to see what's slow."""

import sqlite3
import time
from db import Database
import schema
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.transit import sync_file
import tick
import logging

logging.getLogger().setLevel(logging.CRITICAL)

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

alice = user.new_network(name='Alice', t_ms=1000, db=db)
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)
bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
db.commit()

# Initial sync
for i in range(5):
    tick.tick(t_ms=3000 + i*100, db=db)
    db.commit()

# Create 50 MB file
msg_result = message.create(peer_id=alice['peer_id'], channel_id=alice['channel_id'],
                           content='Test', t_ms=4000, db=db)
file_data = b'X' * (50 * 1024 * 1024)

start_file_create = time.perf_counter()
file_result = message_attachment.create(
    peer_id=alice['peer_id'],
    message_id=msg_result['id'],
    file_data=file_data,
    filename='large.dat',
    mime_type='application/octet-stream',
    t_ms=5000,
    db=db
)
file_create_time = time.perf_counter() - start_file_create
file_id = file_result['file_id']

print(f"File creation time: {file_create_time:.3f}s")
print(f"Slices: {file_result['slice_count']:,}")

sync_file.request_file_sync(file_id=file_id, peer_id=bob['peer_id'],
                            priority=10, ttl_ms=0, t_ms=6000, db=db)
db.commit()

# Profile actual ticks
print(f"\n{'Round':<6} {'Tick Time':<12} {'DB Commit':<12}")
print("-" * 30)

total_tick_time = 0
total_commit_time = 0

for round_num in range(50):
    t_ms = 7000 + round_num * 100

    # Time the entire tick
    tick_start = time.perf_counter()
    tick.tick(t_ms=t_ms, db=db)
    tick_time = time.perf_counter() - tick_start

    # Time the commit
    commit_start = time.perf_counter()
    db.commit()
    commit_time = time.perf_counter() - commit_start

    total_tick_time += tick_time
    total_commit_time += commit_time

    if round_num % 5 == 0:
        print(f"R{round_num:<5} {tick_time*1000:>10.2f}ms {commit_time*1000:>10.2f}ms")

print(f"\nAverage tick time: {total_tick_time/50*1000:.2f}ms")
print(f"Average commit time: {total_commit_time/50*1000:.2f}ms")
