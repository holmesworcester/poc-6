"""
V2 Negentropy Performance Tests - measures sim time and wall time.

Run with: PYTHONPATH=. python tests/test_v2_perf.py
"""
import sqlite3
import time
import logging

# Disable all logging to reduce noise
logging.disable(logging.CRITICAL)

from core.db import Database
from core import schema, transport, tick, simulator
from events.identity import user, invite, peer
from events.content import message, message_attachment
from tests.utils.tick_helper import TestClock

# Test config
TICK_INTERVAL_MS = 100
DEFAULT_LATENCY_MS = 25


def setup_network(db):
    """Create Alice network, Bob joins."""
    clock = TestClock()

    alice = user.new_network(name='Alice', t_ms=clock.tick(), db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=clock.tick(), db=db)

    bob_peer_id = peer.create(t_ms=clock.tick(), db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=clock.now(), db=db)
    db.commit()

    return alice, bob, clock


def run_sync_until(db, check_fn, clock, max_rounds=5000):
    """Run ticks until check passes, return (sim_time_ms, wall_time_s, rounds)."""
    start_wall = time.time()
    start_sim = clock.now()

    for i in range(max_rounds):
        t_ms = clock.tick(TICK_INTERVAL_MS)
        tick.tick(t_ms=t_ms, db=db)
        db.commit()

        if check_fn():
            wall_time = time.time() - start_wall
            sim_time = clock.now() - start_sim
            return sim_time, wall_time, i + 1

    raise TimeoutError(f"Sync not complete after {max_rounds} rounds")


def fresh_db():
    """Create fresh in-memory database with loopback transport."""
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)

    transport.reset()
    transport.enable_loopback()  # Use loopback for faster testing

    return db


def test_1k_messages():
    """Test: Alice creates 1000 messages, Bob syncs them."""
    print("\n" + "="*60)
    print("TEST: 1k Messages Sync")
    print("="*60)

    db = fresh_db()
    alice, bob, clock = setup_network(db)

    # Initial sync to establish connections
    print("Initial sync (establishing connections)...")
    def connections_ready():
        from events.network import connection_request as conn_module
        alice_conns = conn_module.get_connections(alice['peer_id'], clock.now(), db)
        bob_conns = conn_module.get_connections(bob['peer_id'], clock.now(), db)
        return any(c.can_send() for c in alice_conns) and any(c.can_send() for c in bob_conns)

    try:
        sim_ms, wall_s, rounds = run_sync_until(db, connections_ready, clock, max_rounds=500)
        print(f"  Connections ready: {rounds} rounds, {sim_ms}ms sim, {wall_s:.2f}s wall")
    except TimeoutError as e:
        print(f"  ERROR: {e}")
        return

    # Create 1000 messages
    num_messages = 1000
    print(f"\nCreating {num_messages} messages...")
    start = time.time()
    for i in range(num_messages):
        message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content=f'Message {i}',
            t_ms=clock.tick(),
            db=db,
            return_latest=False
        )
    db.commit()
    print(f"  Created in {time.time() - start:.2f}s")

    # Sync messages
    print(f"\nSyncing {num_messages} messages...")
    def bob_has_all():
        count = db.query_one(
            "SELECT COUNT(*) as count FROM messages WHERE recorded_by = ?",
            (bob['peer_id'],)
        )['count']
        return count >= num_messages

    try:
        sim_ms, wall_s, rounds = run_sync_until(db, bob_has_all, clock, max_rounds=2000)
        print(f"\n  ✓ Sync complete:")
        print(f"    Rounds:    {rounds}")
        print(f"    Sim time:  {sim_ms}ms ({sim_ms/1000:.1f}s)")
        print(f"    Wall time: {wall_s:.2f}s")
        print(f"    msgs/sec:  {num_messages/wall_s:.0f}")
    except TimeoutError as e:
        # Check progress
        count = db.query_one(
            "SELECT COUNT(*) as count FROM messages WHERE recorded_by = ?",
            (bob['peer_id'],)
        )['count']
        print(f"  ERROR: {e}")
        print(f"  Progress: Bob has {count}/{num_messages} messages")


def test_file_sync(size_mb):
    """Test: Alice creates file, Bob syncs it."""
    print("\n" + "="*60)
    print(f"TEST: {size_mb}MB File Sync")
    print("="*60)

    db = fresh_db()
    alice, bob, clock = setup_network(db)

    # Initial sync
    print("Initial sync (establishing connections)...")
    def connections_ready():
        from events.network import connection_request as conn_module
        alice_conns = conn_module.get_connections(alice['peer_id'], clock.now(), db)
        bob_conns = conn_module.get_connections(bob['peer_id'], clock.now(), db)
        return any(c.can_send() for c in alice_conns) and any(c.can_send() for c in bob_conns)

    try:
        sim_ms, wall_s, rounds = run_sync_until(db, connections_ready, clock, max_rounds=500)
        print(f"  Connections ready: {rounds} rounds, {sim_ms}ms sim, {wall_s:.2f}s wall")
    except TimeoutError as e:
        print(f"  ERROR: {e}")
        return

    # Create message for attachment
    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content=f'File attachment test',
        t_ms=clock.tick(),
        db=db
    )

    # Create file
    size_bytes = size_mb * 1024 * 1024
    print(f"\nCreating {size_mb}MB file...")
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
    print(f"  Created {slice_count:,} slices in {time.time() - start:.2f}s")

    # Sync file
    print(f"\nSyncing {slice_count:,} slices...")
    def file_complete():
        progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
        return progress and progress['is_complete']

    max_rounds = 5000 if size_mb <= 1 else 20000
    try:
        sim_ms, wall_s, rounds = run_sync_until(db, file_complete, clock, max_rounds=max_rounds)
        print(f"\n  ✓ Sync complete:")
        print(f"    Slices:    {slice_count:,}")
        print(f"    Rounds:    {rounds}")
        print(f"    Sim time:  {sim_ms}ms ({sim_ms/1000:.1f}s)")
        print(f"    Wall time: {wall_s:.2f}s")
        print(f"    slices/sec: {slice_count/wall_s:.0f}")
    except TimeoutError as e:
        progress = message_attachment.get_file_download_progress(file_id, bob['peer_id'], db)
        received = progress['slices_received'] if progress else 0
        print(f"  ERROR: {e}")
        print(f"  Progress: {received}/{slice_count} slices ({100*received/slice_count:.1f}%)")


if __name__ == '__main__':
    test_1k_messages()
    test_file_sync(1)
    test_file_sync(10)
