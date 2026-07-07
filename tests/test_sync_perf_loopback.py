"""
Perf test: loopback sync for a few hundred events.

Run with:
  SYNC_PERF_MESSAGES=300 SYNC_PERF_RUNS=3 pytest -k sync_perf_loopback -s
  SYNC_PERF_DIRECT_SEND=1 SYNC_PERF_RECEIVE_BATCH_SIZE=1000 pytest -k sync_perf_loopback -s
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from statistics import mean, median

from core import crypto, jobs, network_config, schema, tick, transport
from core.db import Database, create_safe_db
from events.content import message
from events.identity import invite, peer, user
from events.network import connection_prekey
from tests.utils.tick_helper import assert_eventually, initial_sync


_DISK_TEST_DIR = Path(__file__).parent.parent / ".test_dbs"
_PERF_SKIP_ENV_KEYS = [
    "SYNC_PERF_SKIP_STORE",
    "SYNC_PERF_SKIP_UNWRAP",
    "SYNC_PERF_SKIP_PROJECTION",
    "SYNC_PERF_SKIP_NEGENTROPY",
    "SYNC_PERF_SKIP_BUCKETS",
    "SYNC_PERF_WRITE_BEHIND",
    "SYNC_PERF_WRITE_BEHIND_MAX_EVENTS",
    "SYNC_PERF_WRITE_BEHIND_MAX_MS",
    "SYNC_PERF_WRITE_BEHIND_FLUSH",
    "SYNC_PERF_FILE_SLICE_MAX_EVENTS",
    "SYNC_PERF_FILE_SLICE_MAX_MS",
    "SYNC_PERF_FILE_SLICE_FLUSH",
]


def _new_db() -> tuple[Database, Path, sqlite3.Connection]:
    _DISK_TEST_DIR.mkdir(exist_ok=True)
    db_path = _DISK_TEST_DIR / f"perf_sync_{uuid.uuid4().hex}.db"
    conn = sqlite3.connect(str(db_path))
    db = Database(conn)

    # Match test defaults for realistic disk perf.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
    conn.execute("PRAGMA temp_store = MEMORY")

    schema.create_all(db)
    return db, db_path, conn


def _cleanup_db(db_path: Path, conn: sqlite3.Connection) -> None:
    conn.close()
    for suffix in ["", "-wal", "-shm"]:
        try:
            os.unlink(str(db_path) + suffix)
        except FileNotFoundError:
            pass


def _reset_globals() -> None:
    jobs.reset_frequency_multiplier()
    network_config.reset_network_config()


def _stash_env(keys: list[str]) -> dict[str, str | None]:
    saved: dict[str, str | None] = {}
    for key in keys:
        saved[key] = os.environ.pop(key, None)
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is not None:
            os.environ[key] = value


def _setup_peers(db: Database) -> tuple[dict, dict]:
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice["peer_id"], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name="Bob", t_ms=2000, db=db)
    db.commit()
    tick.reset_state(db)
    return alice, bob


def _create_messages(db: Database, alice: dict, count: int, start_t_ms: int) -> tuple[int, list[str]]:
    contents = [f"Message {i}" for i in range(count)]
    message_ids = message.batch_create(
        peer_id=alice["peer_id"],
        channel_id=alice["channel_id"],
        contents=contents,
        start_t_ms=start_t_ms,
        db=db,
        skip_projection=False,
    )
    db.commit()
    return start_t_ms + count, message_ids


def _wait_for_sync(
    db: Database,
    bob_peer_id: str,
    count: int,
    start_t_ms: int,
    max_rounds: int,
    interval_ms: int,
) -> None:
    def bob_has_all_messages() -> None:
        row = db.query_one(
            "SELECT COUNT(*) as count FROM messages WHERE recorded_by = ?",
            (bob_peer_id,),
        )
        assert row["count"] >= count, f"Bob has {row['count']}/{count} messages"

    assert_eventually(
        bob_has_all_messages,
        db=db,
        start_t_ms=start_t_ms,
        max_rounds=max_rounds,
        interval_ms=interval_ms,
    )


def _direct_send_events(
    db: Database,
    alice: dict,
    bob: dict,
    event_ids: list[str],
    t_ms: int,
) -> int:
    prekey = connection_prekey.get_connection_prekey_for_peer(
        peer_shared_id=bob["peer_shared_id"],
        recorded_by=alice["peer_id"],
        db=db,
    )
    if not prekey:
        raise AssertionError("No connection prekey for direct send")

    safedb = create_safe_db(db, recorded_by=alice["peer_id"])
    sent = 0
    for idx, event_id in enumerate(event_ids):
        blob = safedb.get_shareable_blob(event_id)
        wrapped = crypto.wrap(blob, prekey, db)
        transport.deliver(wrapped, ('127.0.0.1', 0))
        sent += 1
    return sent


def _wait_for_prekey(
    db: Database,
    alice: dict,
    bob: dict,
    start_t_ms: int,
    max_rounds: int,
    interval_ms: int,
) -> None:
    def has_prekey() -> None:
        prekey = connection_prekey.get_connection_prekey_for_peer(
            peer_shared_id=bob["peer_shared_id"],
            recorded_by=alice["peer_id"],
            db=db,
        )
        assert prekey, "Connection prekey not ready"

    assert_eventually(
        has_prekey,
        db=db,
        start_t_ms=start_t_ms,
        max_rounds=max_rounds,
        interval_ms=interval_ms,
    )


def _run_receive_until(
    db: Database,
    target_count: int,
    start_t_ms: int,
    max_rounds: int,
    interval_ms: int,
) -> None:
    receive_job = jobs.ReceiveJob()
    received = 0

    for i in range(max_rounds):
        t_ms = start_t_ms + i * interval_ms
        result = receive_job.run(t_ms, db)
        db.commit()
        received += result.get("received", 0)
        if received >= target_count:
            return

    raise AssertionError(
        f"receive-only did not reach target: {received}/{target_count} blobs"
    )


def test_sync_perf_loopback() -> None:
    logging.disable(logging.CRITICAL)

    runs = int(os.getenv("SYNC_PERF_RUNS", "3"))
    num_messages = int(os.getenv("SYNC_PERF_MESSAGES", "300"))
    interval_ms = int(os.getenv("SYNC_PERF_INTERVAL_MS", "100"))
    max_rounds = int(os.getenv("SYNC_PERF_MAX_ROUNDS", "200"))
    direct_send = os.getenv("SYNC_PERF_DIRECT_SEND") == "1"

    timings: list[float] = []

    for run_idx in range(runs):
        _reset_globals()
        db, db_path, conn = _new_db()
        try:
            saved_env = _stash_env(_PERF_SKIP_ENV_KEYS)
            try:
                alice, bob = _setup_peers(db)
                sync_ready_t_ms = initial_sync(db, start_t_ms=4000)
                if direct_send:
                    _wait_for_prekey(
                        db,
                        alice,
                        bob,
                        start_t_ms=sync_ready_t_ms,
                        max_rounds=max_rounds,
                        interval_ms=interval_ms,
                    )
            finally:
                _restore_env(saved_env)
            if direct_send:
                transport.reset()

            create_start_t_ms = sync_ready_t_ms + 1000
            create_start = time.perf_counter()
            create_end_t_ms, message_ids = _create_messages(db, alice, num_messages, create_start_t_ms)
            create_elapsed = time.perf_counter() - create_start

            start = time.perf_counter()
            send_elapsed = 0.0
            effective_count = num_messages
            if direct_send:
                send_start = time.perf_counter()
                sent = _direct_send_events(db, alice, bob, message_ids, create_end_t_ms + 1000)
                send_elapsed = time.perf_counter() - send_start
                effective_count = sent
                _run_receive_until(
                    db,
                    target_count=sent,
                    start_t_ms=create_end_t_ms + 1000,
                    max_rounds=max_rounds,
                    interval_ms=interval_ms,
                )
            else:
                _wait_for_sync(
                    db,
                    bob_peer_id=bob["peer_id"],
                    count=num_messages,
                    start_t_ms=create_end_t_ms + 1000,
                    max_rounds=max_rounds,
                    interval_ms=interval_ms,
                )
            elapsed = time.perf_counter() - start
            timings.append(elapsed)
            print(
                f"run {run_idx + 1}/{runs}: {effective_count} msgs sync in "
                f"{elapsed:.3f}s ({effective_count / elapsed:.1f} msgs/s), "
                f"write {create_elapsed:.3f}s, send {send_elapsed:.3f}s"
            )
        finally:
            _cleanup_db(db_path, conn)

    if timings:
        print(
            f"avg={mean(timings):.3f}s median={median(timings):.3f}s "
            f"best={min(timings):.3f}s worst={max(timings):.3f}s"
        )

    assert timings
