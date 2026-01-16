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


def bench_message_sync(mode: str, num_messages: int, use_batch: bool = False) -> dict:
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
    if use_batch:
        contents = [f'Message {i}' for i in range(num_messages)]
        message.batch_create(
            peer_id=alice['peer_id'],
            channel_id=alice['channel_id'],
            contents=contents,
            start_t_ms=3000,
            db=db
        )
    else:
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

    # Debug: verify final counts
    alice_count = db.query_one(
        "SELECT COUNT(*) as c FROM messages WHERE recorded_by = ?",
        (alice['peer_id'],)
    )['c']
    bob_count = count

    cleanup_db(db_path)

    return {
        'mode': mode,
        'messages': num_messages,
        'create_time': create_time,
        'sync_time': sync_time,
        'rounds': rounds,
        'total': create_time + sync_time,
        'alice_count': alice_count,
        'bob_count': bob_count
    }


def main():
    if BENCH_DIR.exists():
        shutil.rmtree(BENCH_DIR)

    print("=" * 70)
    print("Batch vs Individual Creation (small test)")
    print("=" * 70)

    # Just test 500 messages with all 3 methods
    size = 500
    print(f"\n### {size} messages (memory) - CREATE TIME ONLY ###\n")
    print(f"{'Method':<20} {'Create':<10}")
    print("-" * 30)

    r_individual = bench_message_sync("memory", size, use_batch=False)
    print(f"{'individual':<20} {r_individual['create_time']:.2f}s")

    r_batch = bench_message_sync("memory", size, use_batch=True)
    print(f"{'batch+projection':<20} {r_batch['create_time']:.2f}s")

    # Test skip_projection (blob creation only)
    reset_state()
    db, _ = create_db("memory")
    alice = user.new_network(name='Alice', t_ms=1000, db=db)
    db.commit()

    start = time.time()
    contents = [f'Message {i}' for i in range(size)]
    message.batch_create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        contents=contents,
        start_t_ms=3000,
        db=db,
        skip_projection=True
    )
    db.commit()
    skip_proj_time = time.time() - start
    print(f"{'batch+skip_proj':<20} {skip_proj_time:.2f}s")

    print(f"\nProjection overhead: {r_batch['create_time'] - skip_proj_time:.2f}s ({(r_batch['create_time']/skip_proj_time):.1f}x slower with projection)")

    if BENCH_DIR.exists():
        shutil.rmtree(BENCH_DIR)

    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
