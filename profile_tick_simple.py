#!/usr/bin/env python3
"""Simple profile of sync job performance."""

import sqlite3
import time
from db import Database
import schema
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.transit import sync_file
import logging

# Silence logging
logging.getLogger().setLevel(logging.CRITICAL)

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

# Alice creates network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
invite_id, invite_link, invite_data = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)
bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
db.commit()

# Initial sync
from events.transit import sync
for i in range(5):
    sync.send_request_to_all(t_ms=3000 + i*100, db=db)
    sync.receive(batch_size=20, t_ms=3000 + i*100, db=db)
    db.commit()

# Create 50 MB file
msg_result = message.create(peer_id=alice['peer_id'], channel_id=alice['channel_id'],
                           content='Test', t_ms=4000, db=db)
file_data = b'X' * (50 * 1024 * 1024)
file_result = message_attachment.create(
    peer_id=alice['peer_id'],
    message_id=msg_result['id'],
    file_data=file_data,
    filename='large.dat',
    mime_type='application/octet-stream',
    t_ms=5000,
    db=db
)
file_id = file_result['file_id']
print(f"Created 50MB file with {file_result['slice_count']:,} slices")

# Request file
sync_file.request_file_sync(file_id=file_id, peer_id=bob['peer_id'],
                            priority=10, ttl_ms=0, t_ms=6000, db=db)
db.commit()

# Profile sync rounds
print(f"\n{'Round':<6} {'Send':<10} {'Receive':<10} {'Total':<10}")
print("-" * 36)

for round_num in range(15):
    t_ms = 7000 + round_num * 100

    # Measure send_request_to_all
    start = time.perf_counter()
    sync.send_request_to_all(t_ms=t_ms, db=db)
    send_time = time.perf_counter() - start

    # Measure receive
    start = time.perf_counter()
    sync.receive(batch_size=20, t_ms=t_ms, db=db)
    receive_time = time.perf_counter() - start

    db.commit()

    total_time = send_time + receive_time
    print(f"R{round_num:<5} {send_time*1000:>8.1f}ms {receive_time*1000:>9.1f}ms {total_time*1000:>9.1f}ms")
