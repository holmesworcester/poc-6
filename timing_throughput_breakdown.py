#!/usr/bin/env python3
"""Profile sync throughput bottlenecks."""

import time
import os
import sqlite3
import cProfile
import pstats
from io import StringIO

import logging
logging.basicConfig(level=logging.CRITICAL)

from core.db import Database, create_unsafe_db
from core import schema
from core import tick
from core import transport
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.network import negentropy

def run_test():
    print("=== Sync Throughput Bottleneck Analysis ===\n")

    transport.enable_loopback()

    conn = sqlite3.connect(':memory:')
    db = Database(conn)
    schema.create_all(db)
    db.commit()

    base_t_ms = 1769500000000

    # Create users
    alice = user.new_network(name='alice', t_ms=base_t_ms, db=db)
    bob_peer_id = peer.create(t_ms=base_t_ms + 1000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=base_t_ms + 500, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='bob', t_ms=base_t_ms + 1000, db=db)
    db.commit()

    # Initial sync
    t_ms = base_t_ms + 2000
    for i in range(50):
        tick.tick(t_ms, db)
        t_ms += 100
    db.commit()

    # Create 1MB file
    print("Creating 1MB file...")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='File',
        t_ms=t_ms,
        db=db
    )
    t_ms += 100

    file_data = os.urandom(1 * 1024 * 1024)
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename='test.bin',
        mime_type='application/octet-stream',
        t_ms=t_ms,
        db=db
    )
    db.commit()
    print(f"Created {file_result['slice_count']} slices\n")

    unsafedb = create_unsafe_db(db)

    # Measure what happens in each tick
    print("--- Per-tick breakdown (first 20 ticks of sync) ---")
    print(f"{'Tick':>4} {'Time':>8} {'Incoming':>10} {'Processed':>10}")

    t_ms += 100
    for i in range(20):
        # Count queues before
        incoming_before = unsafedb.query_one("SELECT COUNT(*) as c FROM incoming_blobs")['c']

        t0 = time.time()
        tick.tick(t_ms, db)
        elapsed = time.time() - t0

        incoming_after = unsafedb.query_one("SELECT COUNT(*) as c FROM incoming_blobs")['c']

        processed = incoming_before - incoming_after

        print(f"{i:4d} {elapsed*1000:7.1f}ms {incoming_before:10d} {processed:10d}")
        t_ms += 100

    # Profile a batch of ticks
    print("\n--- Profiling 100 sync ticks ---")

    pr = cProfile.Profile()
    pr.enable()

    sync_start = time.time()
    for i in range(100):
        tick.tick(t_ms, db)
        t_ms += 100
    db.commit()
    sync_time = time.time() - sync_start

    pr.disable()

    # Get top time consumers
    s = StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
    ps.print_stats(20)
    print(s.getvalue())

    # Check sync progress
    bob_events = unsafedb._db._conn.execute(
        "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    ).fetchone()[0]

    bob_blob_bytes = unsafedb._db._conn.execute("""
        SELECT SUM(LENGTH(blob)) FROM store
        WHERE id IN (
            SELECT event_id FROM shareable_events
            WHERE can_share_peer_id = ?
        )
    """, (bob['peer_id'],)).fetchone()[0] or 0

    print(f"\n--- After 120 ticks ---")
    print(f"Bob events: {bob_events}")
    print(f"Bob blob bytes: {bob_blob_bytes:,} ({bob_blob_bytes/1024/1024:.2f} MB)")
    print(f"Sync time (100 ticks): {sync_time:.3f}s")
    if sync_time > 0:
        print(f"Throughput: {(bob_blob_bytes/1024/1024)/sync_time:.2f} MB/s")


if __name__ == '__main__':
    run_test()
