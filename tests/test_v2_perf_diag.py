"""
V2 Negentropy Performance Diagnostics - identify bottlenecks.
"""
import sqlite3
import time
import logging
import sys

# Disable all logging to reduce noise
logging.disable(logging.CRITICAL)

from core.db import Database
from core import schema, transport, tick, simulator
from events.identity import user, invite, peer
from events.content import message, message_attachment
from tests.utils.tick_helper import TestClock

# Test config
TICK_INTERVAL_MS = 100


def log(msg):
    """Print with immediate flush."""
    print(msg, flush=True)


def setup_network(db):
    """Create Alice network, Bob joins."""
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)

    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    return alice, bob, clock


def fresh_db():
    """Create fresh in-memory database with loopback transport."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    transport.reset()
    transport.enable_loopback()

    return db


def test_10mb_diagnostic():
    """Diagnostic: Alice creates 10MB file, track sync progress."""
    log("\n" + "="*60)
    log("DIAGNOSTIC: 10MB File Sync")
    log("="*60)

    db = fresh_db()
    alice, bob, clock = setup_network(db)

    # Initial sync to establish connections
    log("Initial sync (establishing connections)...")
    def connections_ready():
        from events.network import connection_request as conn_module
        alice_conns = conn_module.get_connections(alice['peer_id'], clock.now(), db)
        bob_conns = conn_module.get_connections(bob['peer_id'], clock.now(), db)
        return any(c.can_send() for c in alice_conns) and any(c.can_send() for c in bob_conns)

    for i in range(500):
        t_ms = clock.tick(TICK_INTERVAL_MS)
        tick.tick(t_ms=t_ms, db=db)
        db.commit()
        if connections_ready():
            log(f"  Connections ready after {i+1} rounds")
            break
    else:
        log("  ERROR: Connections not ready")
        return

    # Create message for attachment
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='File attachment test',
        t_ms=clock.tick(),
        db=db
    )

    # Create 10MB file
    size_mb = 10
    size_bytes = size_mb * 1024 * 1024
    log(f"\nCreating {size_mb}MB file...")
    start = time.time()

    pattern = b'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n' * 100
    file_data = (pattern * ((size_bytes // len(pattern)) + 1))[:size_bytes]

    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename=f'test_{size_mb}mb.bin',
        mime_type='application/octet-stream',
        t_ms=clock.tick(),
        db=db
    )
    db.commit()

    file_id = file_result['file_id']
    slice_count = file_result['slice_count']
    log(f"  Created {slice_count:,} slices in {time.time() - start:.2f}s")

    # Check how events are distributed
    alice_events = db.query_one(
        "SELECT COUNT(*) as count FROM shareable_events WHERE can_share_peer_id = ?",
        (alice['peer_id'],)
    )['count']
    bob_events = db.query_one(
        "SELECT COUNT(*) as count FROM shareable_events WHERE can_share_peer_id = ?",
        (bob['peer_id'],)
    )['count']
    log(f"\n  Alice has {alice_events:,} shareable events")
    log(f"  Bob has {bob_events:,} shareable events")
    log(f"  Difference: {alice_events - bob_events:,} events to sync")

    # Sync with progress reporting
    log(f"\nSyncing {slice_count:,} slices...")
    log("-" * 40)

    start_wall = time.time()
    last_progress = 0
    last_report_time = start_wall

    for round_num in range(1, 10001):
        round_start = time.time()
        t_ms = clock.tick(TICK_INTERVAL_MS)
        tick.tick(t_ms=t_ms, db=db)
        db.commit()
        round_elapsed = time.time() - round_start

        progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
        current = progress['slices_received'] if progress else 0

        # Report every 5 seconds or on significant progress
        now = time.time()
        if now - last_report_time >= 5.0 or current - last_progress >= 500:
            elapsed = now - start_wall
            rate = current / elapsed if elapsed > 0 else 0
            log(f"  Round {round_num:4d}: {current:5,}/{slice_count:,} ({100*current/slice_count:5.1f}%) - {rate:.0f}/s - last round {round_elapsed*1000:.0f}ms")
            last_progress = current
            last_report_time = now

        if progress and progress['is_complete']:
            elapsed = time.time() - start_wall
            sim_ms = round_num * TICK_INTERVAL_MS
            log("-" * 40)
            log(f"\n  COMPLETE:")
            log(f"    Rounds:     {round_num}")
            log(f"    Sim time:   {sim_ms}ms ({sim_ms/1000:.1f}s)")
            log(f"    Wall time:  {elapsed:.2f}s")
            log(f"    slices/sec: {slice_count/elapsed:.0f}")
            log(f"    rounds/1k:  {round_num*1000/slice_count:.1f}")
            return

    progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
    received = progress['slices_received'] if progress else 0
    log(f"\n  TIMEOUT after 10000 rounds")
    log(f"  Progress: {received}/{slice_count} slices ({100*received/slice_count:.1f}%)")


if __name__ == '__main__':
    test_10mb_diagnostic()
