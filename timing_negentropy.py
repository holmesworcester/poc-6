#!/usr/bin/env python3
"""Timing diagnosis specifically for negentropy operations."""

import time
import os
import sys
import sqlite3

import logging
logging.basicConfig(level=logging.CRITICAL)

from db import Database, create_unsafe_db, create_safe_db
import schema
import tick
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.network import negentropy

def run_test():
    """Profile negentropy operations in isolation."""

    print("=== Negentropy Timing Diagnosis ===\n")

    # Initialize database
    conn = sqlite3.connect(':memory:')
    db = Database(conn)
    schema.create_all(db)
    db.commit()

    # Create users
    alice = user.new_network(name='alice', t_ms=1000, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    invite_id, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='bob', t_ms=2000, db=db)
    db.commit()

    # Initial sync
    t_ms = 3000
    for i in range(50):
        tick.tick(t_ms, db)
        t_ms += 100
    db.commit()

    print(f"Alice peer_id: {alice['peer_id'][:30]}...")
    print(f"Bob peer_id: {bob['peer_id'][:30]}...")

    # Count events before image
    unsafedb = create_unsafe_db(db)
    events_before = unsafedb._db._conn.execute("SELECT COUNT(*) FROM shareable_events").fetchone()[0]
    print(f"\nEvents before image: {events_before}")

    # Measure get_root_hash BEFORE image
    t0 = time.time()
    alice_hash_before = negentropy.get_root_hash(db, alice['peer_id'])
    t_root_before = time.time() - t0
    print(f"get_root_hash (before image): {t_root_before*1000:.2f}ms")

    # Send image (creates 456 slices)
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
    events_after = unsafedb._db._conn.execute("SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?", (alice['peer_id'],)).fetchone()[0]
    print(f"Alice shareable events: {events_after}")

    # Count stale buckets
    stale_count = unsafedb._db._conn.execute("SELECT COUNT(*) FROM negentropy_buckets WHERE hash IS NULL AND recorded_by = ?", (alice['peer_id'],)).fetchone()[0]
    print(f"Alice stale buckets: {stale_count}")

    # Measure get_root_hash AFTER image (first call - must recompute)
    print("\n--- Measuring get_root_hash ---")
    t0 = time.time()
    alice_hash_after = negentropy.get_root_hash(db, alice['peer_id'])
    t_root_first = time.time() - t0
    print(f"get_root_hash (first after image): {t_root_first*1000:.2f}ms")

    # Check stale buckets after recompute
    stale_after = unsafedb._db._conn.execute("SELECT COUNT(*) FROM negentropy_buckets WHERE hash IS NULL AND recorded_by = ?", (alice['peer_id'],)).fetchone()[0]
    print(f"Alice stale buckets after: {stale_after}")

    # Measure get_root_hash SECOND time (should be cached)
    t0 = time.time()
    alice_hash_cached = negentropy.get_root_hash(db, alice['peer_id'])
    t_root_cached = time.time() - t0
    print(f"get_root_hash (cached): {t_root_cached*1000:.2f}ms")

    # Now add ONE more event and see cost
    print("\n--- Adding one more event ---")
    msg2_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='Another message',
        t_ms=t_ms + 100,
        db=db
    )
    db.commit()

    stale_after_msg = unsafedb._db._conn.execute("SELECT COUNT(*) FROM negentropy_buckets WHERE hash IS NULL AND recorded_by = ?", (alice['peer_id'],)).fetchone()[0]
    print(f"Stale buckets after 1 message: {stale_after_msg}")

    t0 = time.time()
    alice_hash_after_msg = negentropy.get_root_hash(db, alice['peer_id'])
    t_root_after_msg = time.time() - t0
    print(f"get_root_hash (after 1 msg): {t_root_after_msg*1000:.2f}ms")

    # Analyze bucket distribution
    print("\n--- Bucket Distribution ---")
    buckets_by_level = unsafedb._db._conn.execute("""
        SELECT level, COUNT(*) as total,
               SUM(CASE WHEN hash IS NULL THEN 1 ELSE 0 END) as stale
        FROM negentropy_buckets
        WHERE recorded_by = ?
        GROUP BY level
        ORDER BY level
    """, (alice['peer_id'],)).fetchall()

    print(f"{'Level':15} {'Total':>8} {'Stale':>8}")
    for row in buckets_by_level:
        print(f"{row[0]:15} {row[1]:8} {row[2]:8}")

    # Check PLACEHOLDER_SYNC
    from events.network import sync as sync_module
    print(f"\nPLACEHOLDER_SYNC = {sync_module.PLACEHOLDER_SYNC}")

    print("\n=== Summary ===")
    print(f"Initial get_root_hash: {t_root_before*1000:.2f}ms (few events)")
    print(f"After image (first): {t_root_first*1000:.2f}ms (recompute 480 events)")
    print(f"After image (cached): {t_root_cached*1000:.2f}ms")
    print(f"After 1 more msg: {t_root_after_msg*1000:.2f}ms")
    print(f"\nRecompute overhead: {t_root_first*1000 - t_root_cached*1000:.2f}ms")


if __name__ == '__main__':
    run_test()
