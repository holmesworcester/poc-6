#!/usr/bin/env python3
"""Focus on sync loop - measure tick time and convergence."""

import time
import os
import sqlite3

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
    print("=== Sync Loop Analysis ===\n")

    transport.enable_loopback()
    transport.set_pacer(None, None)

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
    for i in range(100):
        tick.tick(t_ms, db)
        t_ms += 100
    db.commit()

    unsafedb = create_unsafe_db(db)

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

    file_data = os.urandom(1024 * 1024)
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

    # Count Alice's shareable events (what needs to sync to Bob)
    alice_events = unsafedb._db._conn.execute(
        "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    ).fetchone()[0]
    print(f"Alice shareable events: {alice_events}\n")

    # Track per tick
    print("--- Sync Loop ---")
    print(f"{'Tick':>4} {'Time':>10} {'Bob Events':>12} {'Delta':>8}")

    t_ms += 100
    total_time = 0
    bob_events_prev = 0

    for i in range(100):
        t0 = time.perf_counter()
        tick.tick(t_ms, db)
        elapsed = time.perf_counter() - t0
        total_time += elapsed

        bob_events = unsafedb._db._conn.execute(
            "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?",
            (bob['peer_id'],)
        ).fetchone()[0]

        delta = bob_events - bob_events_prev
        bob_events_prev = bob_events

        if delta > 0 or elapsed > 0.01:
            print(f"{i:4d} {elapsed*1000:9.1f}ms {bob_events:12d} {delta:+8d}")

        t_ms += 100

        # Check convergence
        alice_hash = negentropy.get_root_hash(db, alice['peer_id'])
        bob_hash = negentropy.get_root_hash(db, bob['peer_id'])
        if alice_hash == bob_hash:
            print(f"\n*** Converged at tick {i} ***")
            break

    # Get total bytes synced
    bob_blob_bytes = unsafedb._db._conn.execute("""
        SELECT SUM(LENGTH(blob)) FROM store
        WHERE id IN (
            SELECT event_id FROM shareable_events
            WHERE can_share_peer_id = ?
        )
    """, (bob['peer_id'],)).fetchone()[0] or 0

    print(f"\n--- Summary ---")
    print(f"Total time: {total_time*1000:.1f}ms ({total_time:.3f}s)")
    print(f"Bob events: {bob_events_prev}")
    print(f"Bob bytes: {bob_blob_bytes:,} ({bob_blob_bytes/1024/1024:.2f} MB)")
    if total_time > 0:
        print(f"Throughput: {bob_blob_bytes/1024/1024/total_time:.1f} MB/s")


if __name__ == '__main__':
    run_test()
