"""Run a perf suite to locate bottlenecks across SQLite, sync loop, and full pipeline.

Usage:
  PYTHONPATH=. python3 tests/perf_suite.py --fast
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import statistics
import time

from core import crypto, jobs, network_config, schema, tick, transport, wire_format
from core.db import Database, create_safe_db, create_unsafe_db
from events.content import message, message_attachment
from events.identity import invite, peer, user
from tests.utils import tick_helper
from tests import perf_pipeline_ceiling


def _reset_state() -> None:
    logging.disable(logging.CRITICAL)
    network_config.reset_network_config()
    jobs.reset_frequency_multiplier()
    transport.reset()
    transport.enable_loopback()
    tick_helper.reset_test_clock()


def _apply_perf_tuning(fast: bool) -> None:
    if not fast:
        return
    os.environ.setdefault("SYNC_UPDATE_BATCH", "20000")
    os.environ.setdefault("SYNC_UPDATE_BATCH_MAX", "80000")
    os.environ.setdefault("SYNC_UPDATE_BUDGET_MS", "200")
    os.environ.setdefault("SYNC_UPDATE_BUDGET_MAX", "800")
    os.environ.setdefault("RECEIVE_INSERT_CHUNK", "2000")
    os.environ.setdefault("SQLITE_SYNCHRONOUS", "NORMAL")
    os.environ.setdefault("SQLITE_TEMP_STORE", "MEMORY")
    os.environ.setdefault("SQLITE_CACHE_SIZE", "-131072")  # ~128MB
    os.environ.setdefault("SQLITE_WAL_AUTOCHECKPOINT", "1000")
    jobs.set_frequency_multiplier(0.5)


def _create_db(db_path: str | None) -> Database:
    conn = sqlite3.Connection(db_path or ":memory:")
    db = Database(conn)
    schema.create_all(db)
    return db


def _build_file_slice_payloads(num_slices: int) -> dict[str, list]:
    file_id_bytes = os.urandom(16)
    recorded_by = crypto.b64encode(os.urandom(16))
    file_id = crypto.b64encode(file_id_bytes)
    stored_at = 1_000_000

    event_rows: list[tuple[str, bytes, int]] = []
    recorded_rows: list[tuple[str, bytes, int]] = []
    ingest_rows: list[tuple[int, str, str, str, bytes, str, int]] = []
    file_slice_rows: list[tuple[str, int, bytes, bytes, bytes, str, str, int]] = []
    dep_rows: list[tuple[str, str, str, str]] = []
    shareable_batch: list[tuple[str, int | None, int]] = []

    for i in range(num_slices):
        nonce = bytes([i & 0xFF]) * wire_format.FILE_SLICE_NONCE_SIZE
        ciphertext = bytes([(i + 1) & 0xFF]) * wire_format.FILE_SLICE_CIPHERTEXT_SIZE
        poly_tag = bytes([(i + 2) & 0xFF]) * wire_format.FILE_SLICE_TAG_SIZE
        blob = wire_format.encode_file_slice_wire_event(
            file_id=file_id_bytes,
            slice_number=i,
            nonce=nonce,
            ciphertext=ciphertext,
            poly_tag=poly_tag,
        )
        event_id = crypto.b64encode(crypto.hash(blob))
        event_rows.append((event_id, blob, stored_at))

        recorded_blob = json.dumps({
            "type": "recorded",
            "ref_id": event_id,
            "recorded_by": recorded_by,
        }).encode("utf-8")
        recorded_id = crypto.b64encode(crypto.hash(recorded_blob))
        recorded_rows.append((recorded_id, recorded_blob, stored_at))

        hint = blob[:crypto.KEY_ID_SIZE]
        ingest_rows.append((i + 1, event_id, recorded_id, recorded_by, hint, "file_slice", stored_at))

        file_slice_rows.append((file_id, i, nonce, ciphertext, poly_tag, event_id, recorded_by, stored_at))
        dep_rows.append((event_id, file_id, recorded_by, "file"))
        shareable_batch.append((event_id, None, stored_at))

    return {
        "recorded_by": recorded_by,
        "event_rows": event_rows,
        "recorded_rows": recorded_rows,
        "ingest_rows": ingest_rows,
        "file_slice_rows": file_slice_rows,
        "dep_rows": dep_rows,
        "shareable_batch": shareable_batch,
        "payload_bytes": num_slices * wire_format.FILE_SLICE_CIPHERTEXT_SIZE,
        "wire_bytes": num_slices * wire_format.WIRE_SIZE,
        "slice_count": num_slices,
    }


def _bench_sql_ceiling(num_slices: int, db_path: str | None) -> dict[str, float]:
    from events.network import negentropy

    payloads = _build_file_slice_payloads(num_slices)
    results: dict[str, float] = {}

    def run_variant(name: str, fn) -> None:
        db = _create_db(db_path)
        start = time.perf_counter()
        fn(db)
        elapsed = time.perf_counter() - start
        results[name] = elapsed

    run_variant(
        "store_only",
        lambda db: (
            db._conn.executemany(
                "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
                payloads["event_rows"],
            ),
            db.commit(),
        ),
    )
    run_variant(
        "file_slices_only",
        lambda db: (
            db._conn.executemany(
                """INSERT OR IGNORE INTO file_slices
                   (file_id, slice_number, nonce, ciphertext, poly_tag, event_id, recorded_by, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                payloads["file_slice_rows"],
            ),
            db.commit(),
        ),
    )
    run_variant(
        "full_no_negentropy",
        lambda db: (
            db._conn.executemany(
                "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
                payloads["event_rows"],
            ),
            db._conn.executemany(
                "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
                payloads["recorded_rows"],
            ),
            db._conn.executemany(
                "INSERT OR IGNORE INTO ingest_index "
                "(log_id, event_id, recorded_id, recorded_by, hint, event_type, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                payloads["ingest_rows"],
            ),
            db._conn.executemany(
                """INSERT OR IGNORE INTO file_slices
                   (file_id, slice_number, nonce, ciphertext, poly_tag, event_id, recorded_by, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                payloads["file_slice_rows"],
            ),
            db._conn.executemany(
                """INSERT OR IGNORE INTO event_dependencies
                   (child_event_id, parent_event_id, recorded_by, dependency_type)
                   VALUES (?, ?, ?, ?)""",
                payloads["dep_rows"],
            ),
            db.commit(),
        ),
    )
    run_variant(
        "full_with_negentropy",
        lambda db: (
            db._conn.executemany(
                "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
                payloads["event_rows"],
            ),
            db._conn.executemany(
                "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
                payloads["recorded_rows"],
            ),
            db._conn.executemany(
                "INSERT OR IGNORE INTO ingest_index "
                "(log_id, event_id, recorded_id, recorded_by, hint, event_type, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                payloads["ingest_rows"],
            ),
            db._conn.executemany(
                """INSERT OR IGNORE INTO file_slices
                   (file_id, slice_number, nonce, ciphertext, poly_tag, event_id, recorded_by, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                payloads["file_slice_rows"],
            ),
            db._conn.executemany(
                """INSERT OR IGNORE INTO event_dependencies
                   (child_event_id, parent_event_id, recorded_by, dependency_type)
                   VALUES (?, ?, ?, ?)""",
                payloads["dep_rows"],
            ),
            negentropy.add_shareable_events_batch(
                payloads["shareable_batch"],
                payloads["recorded_by"],
                db,
                defer_buckets=False,
            ),
            db.commit(),
        ),
    )

    payload_mib = payloads["payload_bytes"] / (1024 * 1024)
    for name, elapsed in results.items():
        mib_per_sec = payload_mib / elapsed if elapsed > 0 else 0.0
        results[name] = mib_per_sec
    return results


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


def _bench_sync_loop(size_mb: int, max_rounds: int, db_path: str | None) -> float:
    _reset_state()
    db = _create_db(db_path)

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    _invite_id, invite_link, _invite_data = invite.create(
        peer_id=alice["peer_id"],
        t_ms=1500,
        db=db,
    )
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name="Bob", t_ms=2000, db=db)
    db.commit()

    t_ms = tick_helper.initial_sync(db, start_t_ms=None)

    msg_result = message.create(
        peer_id=alice["peer_id"],
        channel_id=alice["channel_id"],
        content="Sync loop perf bench",
        t_ms=3000,
        db=db,
    )
    file_data = b"X" * (size_mb * 1024 * 1024)
    file_result = message_attachment.create(
        peer_id=alice["peer_id"],
        message_id=msg_result["id"],
        file_data=file_data,
        filename="perf.bin",
        mime_type="application/octet-stream",
        t_ms=4000,
        db=db,
    )
    file_id = file_result["file_id"]
    total_slices = file_result["slice_count"]
    db.commit()

    job_names = {"receive", "sync_respond", "sync_update", "negentropy_sync"}
    start = time.perf_counter()
    safedb = create_safe_db(db, recorded_by=bob["peer_id"])
    start_t_ms = max(t_ms, 5000)

    for i in range(max_rounds):
        t_ms = start_t_ms + i * tick_helper.TICK_INTERVAL_MS
        _run_jobs(t_ms, db, job_names)
        row = safedb.query_one(
            "SELECT COUNT(*) as count FROM file_slices WHERE file_id = ? AND recorded_by = ?",
            (file_id, bob["peer_id"]),
        )
        if row and row["count"] >= total_slices:
            break

    elapsed = time.perf_counter() - start
    mib = (size_mb * 1024 * 1024) / (1024 * 1024)
    return mib / elapsed if elapsed > 0 else 0.0


def _bench_full_pipeline(size_mb: int, max_rounds: int, db_path: str | None) -> float:
    _reset_state()
    db = _create_db(db_path)

    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    _invite_id, invite_link, _invite_data = invite.create(
        peer_id=alice["peer_id"],
        t_ms=1500,
        db=db,
    )
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name="Bob", t_ms=2000, db=db)
    db.commit()

    t_ms = tick_helper.initial_sync(db, start_t_ms=None)

    msg_result = message.create(
        peer_id=alice["peer_id"],
        channel_id=alice["channel_id"],
        content="Full pipeline perf bench",
        t_ms=3000,
        db=db,
    )
    file_data = b"X" * (size_mb * 1024 * 1024)
    file_result = message_attachment.create(
        peer_id=alice["peer_id"],
        message_id=msg_result["id"],
        file_data=file_data,
        filename="perf.bin",
        mime_type="application/octet-stream",
        t_ms=4000,
        db=db,
    )
    file_id = file_result["file_id"]
    db.commit()

    start = time.perf_counter()
    completed = False
    start_t_ms = max(t_ms, 5000)

    for i in range(max_rounds):
        t_ms = start_t_ms + i * tick_helper.TICK_INTERVAL_MS
        tick.tick(t_ms=t_ms, db=db)

        progress = message_attachment.get_file_download_progress(file_id, bob["peer_id"], db)
        if progress and progress["slices_received"] >= progress["total_slices"]:
            completed = True
            break

    if not completed:
        return 0.0
    elapsed = time.perf_counter() - start
    mib = (size_mb * 1024 * 1024) / (1024 * 1024)
    return mib / elapsed if elapsed > 0 else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    return statistics.median(values)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--size-mb", type=int, default=10)
    parser.add_argument("--slices", type=int, default=25000)
    parser.add_argument("--message-count", type=int, default=2000)
    parser.add_argument("--message-bytes", type=int, default=64)
    parser.add_argument("--max-rounds", type=int, default=2000)
    parser.add_argument("--db", type=str, default="")
    args = parser.parse_args()

    _apply_perf_tuning(args.fast)
    db_path = args.db or None

    print("== SQL ceiling (MiB/s payload) ==")
    sql_runs: dict[str, list[float]] = {}
    for _ in range(args.warmup):
        _bench_sql_ceiling(args.slices, db_path)
    for _ in range(args.runs):
        result = _bench_sql_ceiling(args.slices, db_path)
        for name, mib_per_sec in result.items():
            sql_runs.setdefault(name, []).append(mib_per_sec)
    for name, values in sorted(sql_runs.items()):
        print(f"{name:20s} median { _median(values):6.2f} MiB/s (runs={len(values)})")

    print("")
    print("== Sync loop (receive + sync_update + negentropy_sync) ==")
    for _ in range(args.warmup):
        _bench_sync_loop(args.size_mb, args.max_rounds, db_path)
    loop_runs = [
        _bench_sync_loop(args.size_mb, args.max_rounds, db_path)
        for _ in range(args.runs)
    ]
    print(f"median { _median(loop_runs):.2f} MiB/s (runs={len(loop_runs)})")

    print("")
    print("== Full pipeline (all jobs, projection included) ==")
    for _ in range(args.warmup):
        _bench_full_pipeline(args.size_mb, args.max_rounds, db_path)
    full_runs = [
        _bench_full_pipeline(args.size_mb, args.max_rounds, db_path)
        for _ in range(args.runs)
    ]
    print(f"median { _median(full_runs):.2f} MiB/s (runs={len(full_runs)})")

    print("")
    print("== Message pipeline ceiling (non-file events) ==")
    msg_runs = []
    for _ in range(args.warmup):
        perf_pipeline_ceiling.bench_mode(args.message_count, args.message_bytes, args.max_rounds, args.fast)
    for _ in range(args.runs):
        msg_runs.append(
            perf_pipeline_ceiling.bench_mode(
                args.message_count,
                args.message_bytes,
                args.max_rounds,
                args.fast,
            )
        )
    if msg_runs:
        last = msg_runs[-1]
        bytes_total = last["bytes"]
        mb_total = bytes_total / (1024 * 1024)
        receive_mbps = _median([mb_total / r["receive_seconds"] for r in msg_runs])
        materialize_mbps = _median([mb_total / r["materialize_seconds"] for r in msg_runs])
        project_mbps = _median([mb_total / r["project_seconds"] for r in msg_runs])
        print(f"receive median {receive_mbps:.2f} MB/s")
        print(f"sync_update median {materialize_mbps:.2f} MB/s")
        print(f"projection median {project_mbps:.2f} MB/s")


if __name__ == "__main__":
    main()
