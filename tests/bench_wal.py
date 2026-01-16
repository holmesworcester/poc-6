"""
Quick benchmark: WAL vs no-WAL disk performance.

Run with: PYTHONPATH=. python3 tests/bench_wal.py
"""
import sqlite3
import time
import uuid
import os
import shutil
import logging
from pathlib import Path

# Add project root to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

# Disable logging for clean benchmark output
logging.disable(logging.CRITICAL)

from core.db import Database
from core import schema
from events.identity import user, invite, peer
from events.content import message
from core import tick
from core import jobs
from core import network_config

BENCH_DIR = Path(__file__).parent.parent / ".bench_dbs"


def reset_state():
    """Reset global state between runs."""
    network_config.reset_network_config()
    jobs.reset_frequency_multiplier()


def create_db(mode: str) -> tuple[Database, Path]:
    """Create a database with specified mode."""
    BENCH_DIR.mkdir(exist_ok=True)
    db_path = BENCH_DIR / f"bench_{uuid.uuid4().hex}.db"
    conn = sqlite3.connect(str(db_path))
    db = Database(conn)

    if mode == "wal":
        # WAL already enabled in Database.__init__, just add perf pragmas
        conn.execute("PRAGMA synchronous = NORMAL")
    elif mode == "no-wal":
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = NORMAL")
    elif mode == "memory":
        pass  # Already in-memory behavior from Database.__init__

    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA temp_store = MEMORY")
    schema.create_all(db)
    return db, db_path


def cleanup_db(db_path: Path):
    """Clean up database files."""
    for suffix in ["", "-wal", "-shm"]:
        try:
            os.unlink(str(db_path) + suffix)
        except FileNotFoundError:
            pass


def bench_message_sync(mode: str, num_messages: int = 500) -> dict:
    """Benchmark message creation and sync."""
    reset_state()

    if mode == "memory":
        conn = sqlite3.Connection(":memory:")
        db = Database(conn)
        schema.create_all(db)
        db_path = None
    else:
        db, db_path = create_db(mode)

    results = {"mode": mode, "num_messages": num_messages}

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Benchmark message creation
    start = time.time()
    for i in range(num_messages):
        message.create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            content=f'Message {i}',
            t_ms=3000 + i,
            db=db,
            return_latest=False
        )
    db.commit()
    results["create_time"] = time.time() - start

    # Benchmark sync (run ticks until Bob has all messages)
    start = time.time()
    rounds = 0
    max_rounds = 200

    while rounds < max_rounds:
        tick.tick(t_ms=10000 + rounds * 100, db=db)
        db.commit()
        rounds += 1

        count = db.query_one(
            "SELECT COUNT(*) as c FROM messages WHERE recorded_by = ?",
            (bob['peer_id'],)
        )['c']
        if count >= num_messages:
            break

    results["sync_time"] = time.time() - start
    results["sync_rounds"] = rounds

    # Cleanup
    if db_path:
        cleanup_db(db_path)

    return results


def bench_file_sync(mode: str, file_size_kb: int = 100) -> dict:
    """Benchmark file creation and sync."""
    from events.content import message_attachment
    from events.network import sync_file

    reset_state()

    if mode == "memory":
        conn = sqlite3.Connection(":memory:")
        db = Database(conn)
        schema.create_all(db)
        db_path = None
    else:
        db, db_path = create_db(mode)

    results = {"mode": mode, "file_size_kb": file_size_kb}

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Create file
    file_data = b'X' * (file_size_kb * 1024)

    msg_result = message.create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        content='File',
        t_ms=3000,
        db=db
    )

    start = time.time()
    file_result = message_attachment.create(
        peer_id=alice['peer_id'],
        message_id=msg_result['id'],
        file_data=file_data,
        filename='test.dat',
        mime_type='application/octet-stream',
        t_ms=3100,
        db=db
    )
    db.commit()
    results["file_create_time"] = time.time() - start
    results["slice_count"] = file_result['slice_count']

    # Bob requests file
    sync_file.request_file_sync(
        file_id=file_result['file_id'],
        peer_id=bob['peer_id'],
        priority=10,
        ttl_ms=0,
        t_ms=4000,
        db=db
    )
    db.commit()

    # Sync until complete
    start = time.time()
    rounds = 0
    max_rounds = 500

    while rounds < max_rounds:
        tick.tick(t_ms=5000 + rounds * 100, db=db)
        db.commit()
        rounds += 1

        progress = message_attachment.get_file_download_progress(
            file_result['file_id'], bob['peer_id'], db
        )
        if progress and progress['is_complete']:
            break

    results["file_sync_time"] = time.time() - start
    results["file_sync_rounds"] = rounds

    # Cleanup
    if db_path:
        cleanup_db(db_path)

    return results


def main():
    print("=" * 60)
    print("WAL vs No-WAL Benchmark")
    print("=" * 60)

    # Clean up any leftover files
    if BENCH_DIR.exists():
        shutil.rmtree(BENCH_DIR)

    modes = ["memory", "wal", "no-wal"]

    # Message sync benchmark
    print("\n### Message Sync (500 messages) ###\n")
    print(f"{'Mode':<10} {'Create':<10} {'Sync':<10} {'Rounds':<10}")
    print("-" * 40)

    for mode in modes:
        r = bench_message_sync(mode, num_messages=500)
        print(f"{r['mode']:<10} {r['create_time']:.2f}s      {r['sync_time']:.2f}s      {r['sync_rounds']}")

    # File sync benchmark
    print("\n### File Sync (100 KB) ###\n")
    print(f"{'Mode':<10} {'Create':<10} {'Sync':<10} {'Rounds':<10} {'Slices':<10}")
    print("-" * 50)

    for mode in modes:
        r = bench_file_sync(mode, file_size_kb=100)
        print(f"{r['mode']:<10} {r['file_create_time']:.2f}s      {r['file_sync_time']:.2f}s      {r['file_sync_rounds']:<10} {r['slice_count']}")

    # Larger file sync
    print("\n### File Sync (1 MB) ###\n")
    print(f"{'Mode':<10} {'Create':<10} {'Sync':<10} {'Rounds':<10} {'Slices':<10}")
    print("-" * 50)

    for mode in modes:
        r = bench_file_sync(mode, file_size_kb=1024)
        print(f"{r['mode']:<10} {r['file_create_time']:.2f}s      {r['file_sync_time']:.2f}s      {r['file_sync_rounds']:<10} {r['slice_count']}")

    # Cleanup
    if BENCH_DIR.exists():
        shutil.rmtree(BENCH_DIR)

    print("\n" + "=" * 60)
    print("Done!")


if __name__ == "__main__":
    main()
