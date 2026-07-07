#!/usr/bin/env python3
"""Timing diagnosis for negentropy with time+hash bucketing."""

import time
import os
import sqlite3

import logging
logging.basicConfig(level=logging.CRITICAL)

from core.db import Database, create_unsafe_db, create_safe_db
from core import schema
from core import tick
from core import transport
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.network import negentropy

def run_test():
    """Profile negentropy operations with time+hash bucketing."""

    print("=== Negentropy Time+Hash Bucket Timing ===\n")

    # Enable loopback mode for sync testing
    transport.enable_loopback()

    # Initialize database
    conn = sqlite3.connect(':memory:')
    db = Database(conn)
    schema.create_all(db)
    db.commit()

    # Use realistic timestamps (Jan 2026)
    base_t_ms = 1769500000000  # ~Jan 2026

    # Create users
    t0 = time.time()
    alice = user.new_network(name='alice', t_ms=base_t_ms, db=db)
    bob_peer_id = peer.create(t_ms=base_t_ms + 1000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=base_t_ms + 500, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='bob', t_ms=base_t_ms + 1000, db=db)
    db.commit()
    print(f"Users created: {time.time() - t0:.3f}s")

    print(f"Alice peer_id: {alice['peer_id'][:30]}...")
    print(f"Bob peer_id: {bob['peer_id'][:30]}...")

    # Initial sync
    print("\n--- Initial Sync (50 ticks) ---")
    t_ms = base_t_ms + 2000
    t0 = time.time()
    for i in range(50):
        tick.tick(t_ms, db)
        t_ms += 100
    db.commit()
    print(f"Initial sync: {time.time() - t0:.3f}s")

    # Count events before image
    unsafedb = create_unsafe_db(db)
    events_before = unsafedb._db._conn.execute(
        "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    ).fetchone()[0]
    print(f"\nAlice events before image: {events_before}")

    # Measure get_root_hash BEFORE image
    t0 = time.time()
    alice_hash_before = negentropy.get_root_hash(db, alice['peer_id'])
    t_root_before = time.time() - t0
    print(f"get_root_hash (before image): {t_root_before*1000:.2f}ms")

    # Show bucket distribution before image
    print("\nBucket distribution before image:")
    buckets = unsafedb._db._conn.execute("""
        SELECT level, COUNT(*) as total, SUM(event_count) as events
        FROM negentropy_buckets
        WHERE recorded_by = ?
        GROUP BY level ORDER BY level
    """, (alice['peer_id'],)).fetchall()
    print(f"{'Level':15} {'Buckets':>10} {'Events':>10}")
    for row in buckets:
        print(f"{row[0]:15} {row[1]:10} {row[2] or 0:10}")

    # Send image - use larger file for meaningful throughput measurement
    FILE_SIZE_MB = 1  # 1 MB file
    file_size_bytes = FILE_SIZE_MB * 1024 * 1024

    print(f"\n--- Sending {FILE_SIZE_MB}MB File ---")
    t0 = time.time()
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Check out this file!',
        t_ms=t_ms,
        db=db
    )
    t_ms += 100

    file_data = os.urandom(file_size_bytes)
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
    events_after = unsafedb._db._conn.execute(
        "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    ).fetchone()[0]
    print(f"Alice events after image: {events_after} (+{events_after - events_before})")

    # Measure get_root_hash AFTER image
    print("\n--- Root Hash Performance ---")
    t0 = time.time()
    alice_hash_after = negentropy.get_root_hash(db, alice['peer_id'])
    t_root_first = time.time() - t0
    print(f"get_root_hash (after image): {t_root_first*1000:.2f}ms")

    # Second call (should be same - XOR buckets are always current)
    t0 = time.time()
    alice_hash_cached = negentropy.get_root_hash(db, alice['peer_id'])
    t_root_cached = time.time() - t0
    print(f"get_root_hash (second call): {t_root_cached*1000:.2f}ms")

    # Show bucket distribution after image
    print("\nBucket distribution after image:")
    buckets = unsafedb._db._conn.execute("""
        SELECT level, COUNT(*) as total, SUM(event_count) as events
        FROM negentropy_buckets
        WHERE recorded_by = ?
        GROUP BY level ORDER BY level
    """, (alice['peer_id'],)).fetchall()
    print(f"{'Level':15} {'Buckets':>10} {'Events':>10}")
    for row in buckets:
        print(f"{row[0]:15} {row[1]:10} {row[2] or 0:10}")

    # Analyze time bucket clustering for file slices
    print("\n--- Time Bucket Clustering (File Slices) ---")
    # Get unified keys for recent events
    neg_events = unsafedb._db._conn.execute("""
        SELECT unified_key, COUNT(*) as cnt
        FROM negentropy_events
        WHERE recorded_by = ?
        GROUP BY substr(unified_key, 1, 6)
        ORDER BY cnt DESC
        LIMIT 10
    """, (alice['peer_id'],)).fetchall()
    print("Top 10 time buckets by event count:")
    print(f"{'Time Prefix':>12} {'Events':>10}")
    for row in neg_events:
        time_prefix = row[0][:6]
        print(f"{time_prefix:>12} {row[1]:10}")

    # Now sync file to Bob - measure throughput
    print("\n--- Syncing File to Bob (measuring throughput) ---")

    # Get Bob's event count and blob sizes before sync
    bob_events_before = unsafedb._db._conn.execute(
        "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    ).fetchone()[0]

    t_ms += 100
    tick_times = []
    sync_start = time.time()

    # More ticks for larger file
    num_ticks = 500
    for i in range(num_ticks):
        t0 = time.time()
        tick.tick(t_ms, db)
        tick_times.append(time.time() - t0)
        t_ms += 100
    db.commit()

    sync_end = time.time()
    total_sync_time = sync_end - sync_start

    # Calculate bytes received by Bob
    bob_events_after = unsafedb._db._conn.execute(
        "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    ).fetchone()[0]

    # Get total blob size for Bob's events (from store table)
    bob_blob_bytes = unsafedb._db._conn.execute("""
        SELECT SUM(LENGTH(blob)) FROM store
        WHERE id IN (
            SELECT event_id FROM shareable_events
            WHERE can_share_peer_id = ?
        )
    """, (bob['peer_id'],)).fetchone()[0] or 0

    events_received = bob_events_after - bob_events_before

    print(f"Events received by Bob: {events_received}")
    print(f"Total blob bytes: {bob_blob_bytes:,} bytes ({bob_blob_bytes/1024/1024:.2f} MB)")
    print(f"Total sync time: {total_sync_time:.3f}s")
    print(f"Ticks used: {num_ticks}")
    print(f"Avg tick time: {total_sync_time/num_ticks*1000:.2f}ms")
    print(f"Max tick time: {max(tick_times)*1000:.2f}ms")

    # Calculate throughput
    if total_sync_time > 0:
        throughput_mbps = (bob_blob_bytes / 1024 / 1024) / total_sync_time
        print(f"\n*** RECEIVE THROUGHPUT: {throughput_mbps:.2f} MB/s ***")

    # Check Bob's events
    bob_events = unsafedb._db._conn.execute(
        "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    ).fetchone()[0]
    print(f"\nBob events after sync: {bob_events}")

    # Check if sync is complete (root hashes match)
    bob_hash = negentropy.get_root_hash(db, bob['peer_id'])
    alice_hash = negentropy.get_root_hash(db, alice['peer_id'])
    print(f"Alice root hash: {alice_hash.hex()[:32]}...")
    print(f"Bob root hash:   {bob_hash.hex()[:32]}...")
    print(f"Hashes match: {alice_hash == bob_hash}")

    # Final bucket distribution for both peers
    print("\n--- Final Bucket Distribution ---")
    for peer_name, peer_id in [("Alice", alice['peer_id']), ("Bob", bob['peer_id'])]:
        buckets = unsafedb._db._conn.execute("""
            SELECT level, COUNT(*) as total, SUM(event_count) as events
            FROM negentropy_buckets
            WHERE recorded_by = ?
            GROUP BY level ORDER BY level
        """, (peer_id,)).fetchall()
        print(f"\n{peer_name}:")
        print(f"{'Level':15} {'Buckets':>10} {'Events':>10}")
        for row in buckets:
            print(f"{row[0]:15} {row[1]:10} {row[2] or 0:10}")

    print("\n=== Summary ===")
    print(f"File slices created: {file_result['slice_count']}")
    print(f"Time buckets used: {len(neg_events)} (file slices cluster in same time bucket)")
    print(f"Sync completed: {alice_hash == bob_hash}")
    print(f"Total sync time for {events_after} events: {total_sync_time:.3f}s")


if __name__ == '__main__':
    run_test()
