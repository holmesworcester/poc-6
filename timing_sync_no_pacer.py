#!/usr/bin/env python3
"""Full sync throughput test with pacer disabled."""

import time
import os
import sqlite3

import logging
logging.basicConfig(level=logging.WARNING)

from core.db import Database, create_unsafe_db
from core import schema
from core import tick
from core import transport
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.network import negentropy


def run_test():
    print("=== Full Sync Throughput (No Pacer) ===\n")

    transport.enable_loopback()
    # DISABLE pacer - should remove 2 MB/s limit
    transport.set_pacer(None, None)
    print("Pacer disabled\n")

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

    # Initial sync to establish connections
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

    # Get Bob's event count before sync
    bob_events_before = unsafedb._db._conn.execute(
        "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    ).fetchone()[0]

    # Measure sync throughput
    print("--- Syncing File to Bob ---")
    t_ms += 100
    sync_start = time.perf_counter()

    num_ticks = 500
    for i in range(num_ticks):
        tick.tick(t_ms, db)
        t_ms += 100
    db.commit()

    sync_time = time.perf_counter() - sync_start

    # Get Bob's event count after sync
    bob_events_after = unsafedb._db._conn.execute(
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

    events_received = bob_events_after - bob_events_before

    print(f"Events received by Bob: {events_received}")
    print(f"Total blob bytes: {bob_blob_bytes:,} ({bob_blob_bytes/1024/1024:.2f} MB)")
    print(f"Total sync time: {sync_time:.3f}s")
    print(f"Ticks used: {num_ticks}")
    print(f"Avg tick time: {sync_time/num_ticks*1000:.2f}ms")

    if sync_time > 0:
        throughput = (bob_blob_bytes / 1024 / 1024) / sync_time
        print(f"\n*** THROUGHPUT: {throughput:.1f} MB/s ***")

    # Check if sync completed
    alice_hash = negentropy.get_root_hash(db, alice['peer_id'])
    bob_hash = negentropy.get_root_hash(db, bob['peer_id'])
    print(f"\nHashes match: {alice_hash == bob_hash}")


if __name__ == '__main__':
    run_test()
