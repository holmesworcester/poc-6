"""Pipeline ceiling benchmark: receive-only vs materialize vs projection.

Run with:
  PYTHONPATH=. python3 tests/perf_pipeline_ceiling.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time

from core import jobs, network_config, schema, tick, transport
from core.db import Database, create_unsafe_db
from events.content import message
from events.identity import invite, peer, user
from events.network import connection_request


def _reset_state() -> None:
    logging.disable(logging.CRITICAL)
    network_config.reset_network_config()
    jobs.reset_frequency_multiplier()
    transport.reset()
    transport.enable_loopback()


def _apply_perf_tuning(fast: bool) -> None:
    if not fast:
        return
    os.environ["SYNC_UPDATE_BATCH"] = "20000"
    os.environ["SYNC_UPDATE_BATCH_MAX"] = "20000"
    os.environ["SYNC_UPDATE_BUDGET_MS"] = "200"
    os.environ["HIGH_PRIORITY_BATCH"] = "500"
    os.environ["LOW_PRIORITY_BATCH"] = "5000"
    os.environ["JOB_BUDGET_MS_HIGH_PRIORITY_PROJECT"] = "25"
    os.environ["JOB_BUDGET_MS_LOW_PRIORITY_PROJECT"] = "50"
    os.environ["RECEIVE_INSERT_CHUNK"] = "2000"
    os.environ.setdefault("SQLITE_SYNCHRONOUS", "NORMAL")
    os.environ.setdefault("SQLITE_TEMP_STORE", "MEMORY")
    os.environ.setdefault("SQLITE_CACHE_SIZE", "-131072")  # ~128MB (desktop perf)
    os.environ.setdefault("SQLITE_WAL_AUTOCHECKPOINT", "1000")
    jobs.set_frequency_multiplier(0.5)


def _create_db() -> Database:
    conn = sqlite3.Connection(":memory:")
    db = Database(conn)
    schema.create_all(db)
    return db


def _run_jobs(t_ms: int, db: Database, job_names: set[str]) -> None:
    unsafedb = create_unsafe_db(db)
    transport.set_simulator_time(t_ms)

    for job in jobs.JOBS:
        if job.name not in job_names:
            continue
        state = unsafedb.query_one(
            "SELECT last_run_at FROM job_state WHERE job_name = ?",
            (job.name,),
        )
        last_run_at = state['last_run_at'] if state else 0
        if job.should_run(t_ms, last_run_at, db):
            job.run(t_ms, db)
            unsafedb.execute(
                "INSERT OR REPLACE INTO job_state (job_name, last_run_at, updated_at) VALUES (?, ?, ?)",
                (job.name, t_ms, t_ms),
            )
            db.commit()


def _run_until(
    db: Database,
    job_names: set[str],
    start_t_ms: int,
    max_rounds: int,
    done_fn,
) -> tuple[float, int, bool, int]:
    start = time.perf_counter()
    complete = False
    last_t_ms = start_t_ms
    for i in range(max_rounds):
        t_ms = start_t_ms + i * 100
        last_t_ms = t_ms
        _run_jobs(t_ms, db, job_names)
        if done_fn():
            complete = True
            return (time.perf_counter() - start, i + 1, complete, t_ms + 100)
    return (time.perf_counter() - start, max_rounds, complete, last_t_ms + 100)


def _initial_sync(db: Database, rounds: int = 20) -> None:
    for i in range(rounds):
        tick.tick(t_ms=3000 + i * 100, db=db)
        db.commit()


def _get_connection_key_id(db: Database, peer_id: str) -> str:
    row = db.query_one(
        "SELECT key_id FROM connections WHERE recorded_by = ? AND their_key IS NOT NULL AND their_key_id IS NOT NULL LIMIT 1",
        (peer_id,),
    )
    return row['key_id'] if row else ""


def _collect_event_ids(db: Database, peer_id: str, recorded_at_min: int) -> list[str]:
    rows = db.query(
        "SELECT event_id FROM shareable_events WHERE can_share_peer_id = ? AND recorded_at >= ?",
        (peer_id, recorded_at_min),
    )
    return [row['event_id'] for row in rows]


def _total_event_bytes(db: Database, event_ids: list[str]) -> int:
    if not event_ids:
        return 0
    placeholders = ",".join(["?"] * len(event_ids))
    row = db.query_one(
        f"SELECT SUM(LENGTH(blob)) as total FROM store WHERE id IN ({placeholders})",
        tuple(event_ids),
    )
    return int(row['total'] or 0)


def _send_events(db: Database, sender_peer_id: str, conn_key_id: str, event_ids: list[str], t_ms: int) -> None:
    if not event_ids:
        return
    unsafedb = create_unsafe_db(db)
    for event_id in event_ids:
        blob_row = unsafedb.query_one("SELECT blob FROM store WHERE id = ?", (event_id,))
        if not blob_row:
            continue
        connection_request.send(sender_peer_id, conn_key_id, blob_row['blob'], t_ms, db)


def bench_mode(
    num_messages: int,
    content_size: int,
    max_rounds: int,
    fast: bool,
) -> dict[str, float | int | bool | str]:
    _reset_state()
    _apply_perf_tuning(fast)
    db = _create_db()

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    _, invite_link, _ = invite.create(peer_id=alice['peer_id'], t_ms=1500, db=db)
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name="Bob", t_ms=2000, db=db)
    db.commit()

    _initial_sync(db, rounds=25)

    conn_key_id = _get_connection_key_id(db, alice['peer_id'])
    if not conn_key_id:
        _initial_sync(db, rounds=25)
        conn_key_id = _get_connection_key_id(db, alice['peer_id'])

    if not conn_key_id:
        raise RuntimeError("no connection key established")

    content = "m" * content_size
    contents = [content for _ in range(num_messages)]
    start_t_ms = 8000
    message.batch_create(
        peer_id=alice['peer_id'],
        channel_id=alice['channel_id'],
        contents=contents,
        start_t_ms=start_t_ms,
        db=db,
    )
    db.commit()

    event_ids = _collect_event_ids(db, alice['peer_id'], start_t_ms)
    total_bytes = _total_event_bytes(db, event_ids)

    baseline_log = db.query_one(
        "SELECT COUNT(*) as c FROM incoming_event_log WHERE recorded_by = ?",
        (bob['peer_id'],),
    )
    baseline_log_count = baseline_log['c'] if baseline_log else 0

    baseline_index = db.query_one(
        "SELECT COUNT(*) as c FROM ingest_index WHERE recorded_by = ?",
        (bob['peer_id'],),
    )
    baseline_index_count = baseline_index['c'] if baseline_index else 0

    baseline_messages = db.query_one(
        "SELECT COUNT(*) as c FROM messages WHERE recorded_by = ?",
        (bob['peer_id'],),
    )
    baseline_message_count = baseline_messages['c'] if baseline_messages else 0

    _send_events(db, alice['peer_id'], conn_key_id, event_ids, t_ms=9000)

    def done_receive() -> bool:
        row = db.query_one(
            "SELECT COUNT(*) as c FROM incoming_event_log WHERE recorded_by = ?",
            (bob['peer_id'],),
        )
        return (row['c'] if row else 0) >= baseline_log_count + len(event_ids)

    def done_materialize() -> bool:
        row = db.query_one(
            "SELECT COUNT(*) as c FROM ingest_index WHERE recorded_by = ?",
            (bob['peer_id'],),
        )
        return (row['c'] if row else 0) >= baseline_index_count + len(event_ids)

    def done_project() -> bool:
        row = db.query_one(
            "SELECT COUNT(*) as c FROM messages WHERE recorded_by = ?",
            (bob['peer_id'],),
        )
        return (row['c'] if row else 0) >= baseline_message_count + num_messages

    elapsed_receive, rounds_receive, complete_receive, next_t_ms = _run_until(
        db,
        {"receive"},
        start_t_ms=5000,
        max_rounds=max_rounds,
        done_fn=done_receive,
    )

    elapsed_materialize, rounds_materialize, complete_materialize, next_t_ms = _run_until(
        db,
        {"sync_update"},
        start_t_ms=next_t_ms,
        max_rounds=max_rounds,
        done_fn=done_materialize,
    )

    elapsed_project, rounds_project, complete_project, _next_t_ms = _run_until(
        db,
        {"high_priority_project", "low_priority_project"},
        start_t_ms=next_t_ms,
        max_rounds=max_rounds,
        done_fn=done_project,
    )

    return {
        "events": len(event_ids),
        "bytes": total_bytes,
        "receive_seconds": elapsed_receive,
        "receive_rounds": rounds_receive,
        "receive_complete": complete_receive,
        "materialize_seconds": elapsed_materialize,
        "materialize_rounds": rounds_materialize,
        "materialize_complete": complete_materialize,
        "project_seconds": elapsed_project,
        "project_rounds": rounds_project,
        "project_complete": complete_project,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline ceiling benchmark")
    parser.add_argument("--message-count", type=int, default=5000)
    parser.add_argument("--message-bytes", type=int, default=64)
    parser.add_argument("--max-rounds", type=int, default=200)
    parser.add_argument("--fast", action="store_true")
    # Legacy wrapped-receive mode removed
    args = parser.parse_args()

    result = bench_mode(
        args.message_count,
        args.message_bytes,
        args.max_rounds,
        args.fast,
    )

    total_bytes = result["bytes"]

    receive_rate_mb = (total_bytes / (1024 * 1024)) / result["receive_seconds"] if result["receive_seconds"] > 0 else 0.0
    materialize_rate_mb = (total_bytes / (1024 * 1024)) / result["materialize_seconds"] if result["materialize_seconds"] > 0 else 0.0
    project_rate_mb = (total_bytes / (1024 * 1024)) / result["project_seconds"] if result["project_seconds"] > 0 else 0.0

    print(
        f"jobs=receive: {result['events']} events in {result['receive_seconds']:.2f}s "
        f"({receive_rate_mb:.2f} MB/s, rounds={result['receive_rounds']}, complete={result['receive_complete']})"
    )
    print(
        f"jobs=sync_update: {result['events']} events in {result['materialize_seconds']:.2f}s "
        f"({materialize_rate_mb:.2f} MB/s, rounds={result['materialize_rounds']}, complete={result['materialize_complete']})"
    )
    print(
        f"jobs=high_priority_project+low_priority_project: {result['events']} events in {result['project_seconds']:.2f}s "
        f"({project_rate_mb:.2f} MB/s, rounds={result['project_rounds']}, complete={result['project_complete']})"
    )


if __name__ == "__main__":
    main()
