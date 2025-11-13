#!/usr/bin/env python3
"""Profile tick() to see which jobs are slowest."""

import sqlite3
import time
from db import Database
import schema
from events.identity import user, invite, peer
from events.content import message
from events.transit import sync_file
import tick
import jobs
import logging

# Disable verbose logging
logging.basicConfig(level=logging.WARNING)

# Setup
conn = sqlite3.Connection(":memory:")
db = Database(conn)
schema.create_all(db)

print("=== Setup: Create network ===")

# Alice creates network
alice = user.new_network(name='Alice', t_ms=1000, db=db)
print(f"✓ Alice created network")

# Alice creates invite
invite_id, invite_link, invite_data = invite.create(
    peer_id=alice['peer_id'],
    t_ms=1500,
    db=db
)

# Bob joins
bob_peer_id, bob_peer_shared_id = peer.create(t_ms=2000, db=db)
bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
print(f"✓ Bob joined network")

db.commit()

# Initial sync
print("\n=== Initial sync (5 ticks) ===")
for i in range(5):
    tick.tick(t_ms=3000 + i*100, db=db)
    db.commit()
print("✓ Initial sync completed")

# Create 50 MB file
print("\n=== Create 50 MB file ===")
msg_result = message.create(
    peer_id=alice['peer_id'],
    channel_id=alice['channel_id'],
    content='Large file test',
    t_ms=4000,
    db=db
)
message_id = msg_result['id']

file_size = 50 * 1024 * 1024  # 50 MB
file_data = b'X' * file_size
print(f"✓ Created {file_size:,} byte file")

# Import message_attachment after we know we need it
from events.content import message_attachment
file_result = message_attachment.create(
    peer_id=alice['peer_id'],
    message_id=message_id,
    file_data=file_data,
    filename='large_file.dat',
    mime_type='application/octet-stream',
    t_ms=5000,
    db=db
)
file_id = file_result['file_id']
slice_count = file_result['slice_count']
print(f"✓ Alice created file: {slice_count:,} slices")

db.commit()

# Request file sync
print("\n=== Bob requests file sync ===")
sync_file.request_file_sync(
    file_id=file_id,
    peer_id=bob['peer_id'],
    priority=10,
    ttl_ms=0,
    t_ms=6000,
    db=db
)
db.commit()
print(f"✓ File sync requested")

# Now profile ticks with active file sync
print("\n=== Profiling ticks with active file sync ===")
print(f"{'Round':<6} {'Total':>8} {' '.join(f'{j.name:<20}'[:20] for j in jobs.JOBS)}")
print("-" * (14 + sum(20 for _ in jobs.JOBS)))

job_totals = {job.name: 0 for job in jobs.JOBS}

for round_num in range(20):
    current_time_ms = 7000 + round_num * 100

    # Measure total tick time
    tick_start = time.perf_counter()

    # Manually run jobs to profile each one
    from db import create_unsafe_db
    unsafedb = create_unsafe_db(db)

    job_times = []
    for job in jobs.JOBS:
        last_run_at = unsafedb.query_one(
            "SELECT last_run_at FROM job_state WHERE job_name = ?",
            (job.name,)
        )
        last_run_at = last_run_at['last_run_at'] if last_run_at else 0

        if job.should_run(current_time_ms, last_run_at, db):
            job_start = time.perf_counter()
            try:
                job.run(current_time_ms, db)
            except Exception as e:
                print(f"Job {job.name} failed: {e}")
            job_elapsed = time.perf_counter() - job_start
            job_times.append((job.name, job_elapsed))
            job_totals[job.name] += job_elapsed
        else:
            job_times.append((job.name, 0))

    db.commit()
    tick_elapsed = time.perf_counter() - tick_start

    # Format output
    time_parts = [f"{tick_elapsed*1000:>7.1f}ms"]
    for job in jobs.JOBS:
        t = next((t for name, t in job_times if name == job.name), 0)
        time_parts.append(f"{t*1000:>19.1f}ms")

    print(f"R{round_num:<5} " + " ".join(time_parts))

print("\n=== Job Total Times ===")
total = sum(job_totals.values())
for job_name, job_time in sorted(job_totals.items(), key=lambda x: -x[1]):
    if job_time > 0:
        pct = (job_time / total * 100) if total > 0 else 0
        print(f"{job_name:<40} {job_time*1000:>8.1f}ms ({pct:>5.1f}%)")
