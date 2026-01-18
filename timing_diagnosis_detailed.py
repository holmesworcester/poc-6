#!/usr/bin/env python3
"""Detailed timing diagnosis for sync_receive and negentropy."""

import time
import os
import sys
import sqlite3

# Configure minimal logging to avoid noise
import logging
logging.basicConfig(level=logging.CRITICAL)

from db import Database, create_unsafe_db
import schema
import tick
import jobs
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.network import negentropy

def run_timed_test():
    """Run a two-user image sync with detailed timing."""

    print("=== Detailed Sync Timing Diagnosis ===\n")

    # Initialize database
    conn = sqlite3.connect(':memory:')
    db = Database(conn)
    schema.create_all(db)
    db.commit()

    # Create users
    t0 = time.time()
    alice = user.new_network(name='alice', t_ms=1000, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='bob', t_ms=2000, db=db)
    db.commit()
    print(f"Users created: {time.time() - t0:.3f}s")

    # Initial sync
    print("\n--- Initial Sync ---")
    t_ms = 3000
    for i in range(50):
        tick.tick(t_ms, db)
        t_ms += 100
    db.commit()

    # Count events before image
    unsafedb = create_unsafe_db(db)
    # Use raw SQL for subjective tables (just counting)
    events_before = unsafedb._db._conn.execute("SELECT COUNT(*) FROM shareable_events").fetchone()[0]
    incoming_before = unsafedb.query_one("SELECT COUNT(*) as c FROM incoming_blobs")['c']
    print(f"Events before image: {events_before}, incoming queue: {incoming_before}")

    # Send image
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

    # Count events after image
    events_after = unsafedb._db._conn.execute("SELECT COUNT(*) FROM shareable_events").fetchone()[0]
    alice_shareable = unsafedb._db._conn.execute("SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],)).fetchone()[0]
    bob_shareable = unsafedb._db._conn.execute("SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?", (bob['peer_id'],)).fetchone()[0]
    print(f"Total events after image: {events_after} (alice: {alice_shareable}, bob: {bob_shareable})")

    # Track per-tick metrics
    print("\n--- Image Sync (50 ticks with detailed metrics) ---")
    tick_data = []

    for i in range(50):
        t_ms += 100
        tick_start = time.time()

        # Count incoming blobs before tick
        incoming_before_tick = unsafedb.query_one("SELECT COUNT(*) as c FROM incoming_blobs")['c']

        tick.tick(t_ms, db)

        # Count incoming blobs after tick
        incoming_after_tick = unsafedb.query_one("SELECT COUNT(*) as c FROM incoming_blobs")['c']

        tick_elapsed = time.time() - tick_start

        tick_data.append({
            'tick': i,
            'elapsed_ms': tick_elapsed * 1000,
            'incoming_before': incoming_before_tick,
            'incoming_after': incoming_after_tick,
            'processed': incoming_before_tick - incoming_after_tick,
            'added': max(0, incoming_after_tick - (incoming_before_tick - (incoming_before_tick - incoming_after_tick))),
        })

    db.commit()

    # Print per-tick data (just first 20)
    print("\nPer-tick breakdown (first 20):")
    print(f"{'Tick':>4} {'Time':>8} {'Queue Before':>12} {'Queue After':>12} {'Processed':>10}")
    for d in tick_data[:20]:
        print(f"{d['tick']:4d} {d['elapsed_ms']:7.1f}ms {d['incoming_before']:12d} {d['incoming_after']:12d} {d['processed']:10d}")

    # Check event creation pattern
    print("\n--- Event Creation Analysis ---")
    # Check how many new events are created per tick
    final_alice_shareable = unsafedb._db._conn.execute("SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],)).fetchone()[0]
    final_bob_shareable = unsafedb._db._conn.execute("SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?", (bob['peer_id'],)).fetchone()[0]
    print(f"Final: alice {final_alice_shareable} events, bob {final_bob_shareable} events")

    # Check if prekeys or other auto-created events are being created
    all_events = unsafedb._db._conn.execute("""
        SELECT can_share_peer_id as recorded_by, COUNT(*) as count
        FROM shareable_events
        GROUP BY can_share_peer_id
    """).fetchall()
    print("\nEvents per peer:")
    for row in all_events:
        print(f"  {row['recorded_by'][:20]}...: {row['count']} events")

    # Check negentropy bucket counts
    neg_buckets = unsafedb._db._conn.execute("SELECT recorded_by, COUNT(*) as c FROM negentropy_buckets GROUP BY recorded_by").fetchall()
    print("\nNegentropy buckets per peer:")
    for row in neg_buckets:
        print(f"  {row[0][:20]}...: {row[1]} buckets")

    # Check sync state
    sync_states_count = unsafedb._db._conn.execute("SELECT COUNT(*) FROM negentropy_sync_state").fetchone()[0]
    print(f"\nNegentropy sync states: {sync_states_count} entries")

    # Check for root hash recomputation
    stale_buckets = unsafedb._db._conn.execute("SELECT recorded_by, level, COUNT(*) as c FROM negentropy_buckets WHERE hash IS NULL GROUP BY recorded_by, level").fetchall()
    if stale_buckets:
        print("\nStale (NULL hash) buckets:")
        for row in stale_buckets:
            print(f"  {row[0][:20]}... level={row[1]}: {row[2]} stale")
    else:
        print("\nNo stale buckets (all hashes computed)")


if __name__ == '__main__':
    run_timed_test()
