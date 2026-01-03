#!/usr/bin/env python3
"""Timing diagnosis script for sync slowness."""

import time
import os
import sys
import sqlite3

# Configure minimal logging to avoid noise
import logging
logging.basicConfig(level=logging.CRITICAL)

from db import Database
import schema
import tick
import jobs
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.network import sync as sync_module, negentropy

def run_timed_test():
    """Run a two-user image sync with detailed timing."""

    print("=== Sync Timing Diagnosis ===\n")

    # Initialize database
    conn = sqlite3.connect(':memory:')
    db = Database(conn)
    schema.create_all(db)
    db.commit()

    # Step 1: Create users
    t0 = time.time()
    alice = user.new_network(name='alice', t_ms=1000, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='bob', t_ms=2000, db=db)
    db.commit()
    print(f"Users created: {time.time() - t0:.3f}s")

    # Step 2: Initial sync (like run_auto_tick)
    print("\n--- Initial Sync (100 ticks) ---")
    t_ms = 3000
    tick_times = []
    job_total_times = {job.name: 0.0 for job in jobs.JOBS}

    for i in range(100):
        tick_start = time.time()

        # Run each job with timing
        for job in jobs.JOBS:
            job_start = time.time()
            tick.tick(t_ms, db)  # This runs ALL jobs
            job_elapsed = time.time() - job_start
            break  # tick runs all jobs, so we only need to call once

        tick_elapsed = time.time() - tick_start
        tick_times.append(tick_elapsed)
        t_ms += 100

    db.commit()
    total_initial = sum(tick_times)
    print(f"Total: {total_initial:.3f}s ({sum(tick_times)/len(tick_times)*1000:.1f}ms per tick)")

    # Step 3: Send image
    print("\n--- Sending Image ---")
    t0 = time.time()
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Check out this image!',
        t_ms=t_ms,
        db=db
    )
    t_ms += 100

    file_data = os.urandom(200 * 1024)  # 200KB
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename='test-image.png',
        mime_type='image/png',
        t_ms=t_ms,
        db=db
    )
    db.commit()
    print(f"Image created: {time.time() - t0:.3f}s ({file_result['slice_count']} slices)")

    # Step 4: Sync to deliver image (100 ticks like auto-tick)
    print("\n--- Image Sync (100 ticks) ---")

    # Measure per-job times
    job_times = {job.name: [] for job in jobs.JOBS}
    tick_times = []

    for i in range(100):
        tick_start = time.time()
        t_ms += 100

        # Manually run each job with timing
        from db import create_unsafe_db
        unsafedb = create_unsafe_db(db)

        for job in jobs.JOBS:
            state = unsafedb.query_one(
                "SELECT last_run_at FROM job_state WHERE job_name = ?",
                (job.name,)
            )
            last_run_at = state['last_run_at'] if state else 0

            if job.should_run(t_ms, last_run_at, db):
                job_start = time.time()
                job.run(t_ms, db)
                job_elapsed = time.time() - job_start
                job_times[job.name].append(job_elapsed)

                # Update state
                unsafedb.execute(
                    """INSERT OR REPLACE INTO job_state (job_name, last_run_at, updated_at)
                       VALUES (?, ?, ?)""",
                    (job.name, t_ms, t_ms)
                )
                db.commit()

        tick_elapsed = time.time() - tick_start
        tick_times.append(tick_elapsed)

    # Report
    total_sync = sum(tick_times)
    print(f"\nTotal: {total_sync:.3f}s ({sum(tick_times)/len(tick_times)*1000:.1f}ms per tick)")

    print("\n--- Per-Job Times ---")
    for name, times in sorted(job_times.items(), key=lambda x: -sum(x[1])):
        if times:
            total = sum(times)
            avg = total / len(times) * 1000
            print(f"  {name:30} {total:.3f}s total ({avg:.1f}ms avg, {len(times)} runs)")

    # Check if Bob got the file
    progress = message_attachment.get_file_download_progress(
        file_result['file_id'], bob['peer_id'], db
    )
    print(f"\nBob's file progress: {progress['percentage_complete']}% ({progress['slices_received']}/{progress['total_slices']} slices)")

    # Count events
    from db import create_safe_db
    safedb_alice = create_safe_db(db, recorded_by=alice['peer_id'])
    safedb_bob = create_safe_db(db, recorded_by=bob['peer_id'])

    alice_events = safedb_alice.query_one("SELECT COUNT(*) as c FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],))['c']
    bob_events = safedb_bob.query_one("SELECT COUNT(*) as c FROM shareable_events WHERE can_share_peer_id = ?", (bob['peer_id'],))['c']
    alice_neg = safedb_alice.query_one("SELECT COUNT(*) as c FROM negentropy_events WHERE recorded_by = ?", (alice['peer_id'],))['c']
    bob_neg = safedb_bob.query_one("SELECT COUNT(*) as c FROM negentropy_events WHERE recorded_by = ?", (bob['peer_id'],))['c']

    print(f"\nAlice shareable events: {alice_events}, negentropy events: {alice_neg}")
    print(f"Bob shareable events: {bob_events}, negentropy events: {bob_neg}")

    # Check incoming queue
    from db import create_unsafe_db
    unsafedb = create_unsafe_db(db)
    queue_count = unsafedb.query_one("SELECT COUNT(*) as c FROM incoming_blobs")['c']
    print(f"Incoming queue size: {queue_count}")

    # Total time
    print(f"\n=== TOTAL: {total_initial + total_sync:.3f}s ===")


if __name__ == '__main__':
    run_timed_test()
