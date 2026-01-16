"""
Quick scaling benchmark to estimate slow test times.

Run with: PYTHONPATH=. python3 tests/bench_scaling.py
"""
import sqlite3
import time
import uuid
import os
import shutil
import logging
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

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
    network_config.reset_network_config()
    jobs.reset_frequency_multiplier()


def create_db(mode: str) -> tuple[Database, Path | None]:
    if mode == "memory":
        conn = sqlite3.Connection(":memory:")
        db = Database(conn)
        schema.create_all(db)
        return db, None

    BENCH_DIR.mkdir(exist_ok=True)
    db_path = BENCH_DIR / f"bench_{uuid.uuid4().hex}.db"
    conn = sqlite3.connect(str(db_path))
    db = Database(conn)

    if mode == "wal":
        conn.execute("PRAGMA synchronous = NORMAL")
    elif mode == "no-wal":
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = NORMAL")

    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA temp_store = MEMORY")
    schema.create_all(db)
    return db, db_path


def cleanup_db(db_path: Path | None):
    if db_path:
        for suffix in ["", "-wal", "-shm"]:
            try:
                os.unlink(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def bench_message_sync(mode: str, num_messages: int) -> dict:
    reset_state()
    db, db_path = create_db(mode)

    # Setup
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name='Bob', t_ms=2000, db=db)
    db.commit()

    # Create messages
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
    create_time = time.time() - start

    # Sync
    start = time.time()
    rounds = 0
    max_rounds = 500

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

    sync_time = time.time() - start
    cleanup_db(db_path)

    return {
        'mode': mode,
        'messages': num_messages,
        'create_time': create_time,
        'sync_time': sync_time,
        'rounds': rounds,
        'total': create_time + sync_time
    }


def main():
    if BENCH_DIR.exists():
        shutil.rmtree(BENCH_DIR)

    print("=" * 70)
    print("Message Sync Scaling Benchmark")
    print("=" * 70)

    sizes = [100, 500, 1000, 2000]
    modes = ["memory", "wal", "no-wal"]

    results = {mode: [] for mode in modes}

    for size in sizes:
        print(f"\n### {size} messages ###\n")
        print(f"{'Mode':<10} {'Create':<10} {'Sync':<10} {'Rounds':<8} {'Total':<10}")
        print("-" * 48)

        for mode in modes:
            r = bench_message_sync(mode, size)
            results[mode].append(r)
            print(f"{r['mode']:<10} {r['create_time']:.2f}s      {r['sync_time']:.2f}s      {r['rounds']:<8} {r['total']:.2f}s")

    # Estimate 10k
    print("\n" + "=" * 70)
    print("Estimates for 10,000 messages (linear extrapolation from 2000):")
    print("=" * 70 + "\n")

    for mode in modes:
        r2k = results[mode][-1]  # 2000 message result
        scale = 10000 / 2000
        est_create = r2k['create_time'] * scale
        est_sync = r2k['sync_time'] * scale
        est_total = est_create + est_sync
        print(f"{mode:<10}: ~{est_total:.0f}s total (~{est_create:.0f}s create + ~{est_sync:.0f}s sync)")

    if BENCH_DIR.exists():
        shutil.rmtree(BENCH_DIR)

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
