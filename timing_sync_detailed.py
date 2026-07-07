#!/usr/bin/env python3
"""Detailed sync pattern analysis."""

import time
import os
import sqlite3

import logging
logging.basicConfig(level=logging.CRITICAL)  # Suppress all warnings

from core.db import Database, create_unsafe_db
from core import schema
from core import tick
from core import transport
from events.identity import user, invite, peer
from events.content import message, message_attachment
from events.network import negentropy


def run_test():
    print("=== Detailed Sync Pattern Analysis ===\n")

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

    # Create 500KB file (smaller for faster test)
    print("Creating 500KB file...")
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='File',
        t_ms=t_ms,
        db=db
    )
    t_ms += 100

    file_data = os.urandom(500 * 1024)
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
    slice_count = file_result['slice_count']
    print(f"Created {slice_count} slices\n")

    # Track metrics per tick
    print("--- Per-tick Analysis (first 50 ticks) ---")
    print(f"{'Tick':>4} {'Time':>8} {'Incoming':>10} {'Blocked':>10} {'Bob Events':>12} {'Outgoing':>10}")

    t_ms += 100
    for i in range(50):
        # Counts before tick
        incoming_before = unsafedb._db._conn.execute(
            "SELECT COUNT(*) FROM incoming_event_log"
        ).fetchone()[0]

        blocked_before = unsafedb._db._conn.execute(
            "SELECT COUNT(*) FROM blocked_events_ephemeral"
        ).fetchone()[0]

        bob_events_before = unsafedb._db._conn.execute(
            "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?",
            (bob['peer_id'],)
        ).fetchone()[0]

        outgoing_before = transport.pending_count()[1]

        t0 = time.perf_counter()
        tick.tick(t_ms, db)
        elapsed = time.perf_counter() - t0

        # Counts after tick
        incoming_after = unsafedb._db._conn.execute(
            "SELECT COUNT(*) FROM incoming_event_log"
        ).fetchone()[0]

        blocked_after = unsafedb._db._conn.execute(
            "SELECT COUNT(*) FROM blocked_events_ephemeral"
        ).fetchone()[0]

        bob_events_after = unsafedb._db._conn.execute(
            "SELECT COUNT(*) FROM shareable_events WHERE can_share_peer_id = ?",
            (bob['peer_id'],)
        ).fetchone()[0]

        outgoing_after = transport.pending_count()[1]

        # Deltas
        incoming_delta = incoming_after - incoming_before
        blocked_delta = blocked_after - blocked_before
        bob_delta = bob_events_after - bob_events_before
        outgoing_delta = outgoing_after - outgoing_before

        print(f"{i:4d} {elapsed*1000:7.1f}ms {incoming_delta:+10d} {blocked_delta:+10d} {bob_delta:+12d} {outgoing_delta:+10d}")

        t_ms += 100

    # Continue sync until complete
    print("\n--- Continuing sync to completion ---")
    sync_start = time.perf_counter()
    ticks_to_converge = 0

    while True:
        tick.tick(t_ms, db)
        t_ms += 100
        ticks_to_converge += 1

        if ticks_to_converge > 1000:
            print("Sync not converging, stopping")
            break

        alice_hash = negentropy.get_root_hash(db, alice['peer_id'])
        bob_hash = negentropy.get_root_hash(db, bob['peer_id'])
        if alice_hash == bob_hash:
            break

    sync_time = time.perf_counter() - sync_start

    bob_blob_bytes = unsafedb._db._conn.execute("""
        SELECT SUM(LENGTH(blob)) FROM store
        WHERE id IN (
            SELECT event_id FROM shareable_events
            WHERE can_share_peer_id = ?
        )
    """, (bob['peer_id'],)).fetchone()[0] or 0

    print(f"\nSync converged after {50 + ticks_to_converge} total ticks")
    print(f"Additional sync time: {sync_time:.2f}s")
    print(f"Bob blob bytes: {bob_blob_bytes:,} ({bob_blob_bytes/1024/1024:.2f} MB)")

    blocked_final = unsafedb._db._conn.execute(
        "SELECT COUNT(*) FROM blocked_events_ephemeral"
    ).fetchone()[0]
    print(f"Blocked events remaining: {blocked_final}")


if __name__ == '__main__':
    run_test()
