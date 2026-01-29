"""Perf bench: negentropy sync loop throughput (receive + sync_update, no projection).

Run with:
  PYTHONPATH=. python3 tests/perf_sync_loop_ceiling.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time

from core import jobs, network_config, schema, tick, transport, wire_format
from core.db import Database, create_safe_db, create_unsafe_db
from events.content import message, message_attachment
from events.identity import invite, peer, user
from events.network import negentropy
from tests.utils import tick_helper


def _reset_state() -> None:
    timing_enabled = os.getenv("NEGENTROPY_TIMING", "").lower() in ("1", "true", "yes")
    network_config.reset_network_config()
    jobs.reset_frequency_multiplier()
    transport.reset()
    transport.enable_loopback()
    if not timing_enabled:
        logging.disable(logging.CRITICAL)
        return
    # Keep only negentropy timing logs.
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO)
    root.setLevel(logging.INFO)
    for handler in root.handlers:
        handler.addFilter(lambda record: record.name.startswith("events.network.negentropy"))
    logging.getLogger("events.network.negentropy").setLevel(logging.INFO)


def _apply_perf_tuning(fast: bool) -> None:
    if not fast:
        return
    os.environ["SYNC_UPDATE_BATCH"] = os.getenv("SYNC_UPDATE_BATCH", "20000")
    os.environ["SYNC_UPDATE_BATCH_MAX"] = os.getenv("SYNC_UPDATE_BATCH_MAX", "80000")
    os.environ["SYNC_UPDATE_BUDGET_MS"] = os.getenv("SYNC_UPDATE_BUDGET_MS", "200")
    os.environ["SYNC_UPDATE_BUDGET_MAX"] = os.getenv("SYNC_UPDATE_BUDGET_MAX", "800")
    os.environ["RECEIVE_INSERT_CHUNK"] = os.getenv("RECEIVE_INSERT_CHUNK", "2000")
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=10)
    parser.add_argument("--max-rounds", type=int, default=2000)
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--db", type=str, default="")
    args = parser.parse_args()

    _reset_state()
    _apply_perf_tuning(args.fast)
    db = _create_db(args.db or None)

    # Setup network + peers
    alice = user.new_network(name="Alice", t_ms=1000, db=db)
    _invite_id, invite_link, _invite_data = invite.create(
        peer_id=alice["peer_id"],
        t_ms=1500,
        db=db,
    )
    bob_peer_id = peer.create(t_ms=2000, db=db)
    bob = user.join(peer_id=bob_peer_id, invite_link=invite_link, name="Bob", t_ms=2000, db=db)
    db.commit()

    # Initial sync to establish connection
    t_ms = tick_helper.initial_sync(db, start_t_ms=None)

    # Alice creates message + attachment
    msg_result = message.create(
        peer_id=alice["peer_id"],
        channel_id=alice["channel_id"],
        content="Sync loop perf bench",
        t_ms=3000,
        db=db,
    )
    file_data = b"X" * (args.size_mb * 1024 * 1024)
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

    print(f"Created {args.size_mb}MB file with {total_slices} slices")

    job_names = {
        "receive",
        "sync_respond",
        "sync_update",
        "negentropy_sync",
        "high_priority_project",
        "low_priority_project",
    }

    start = time.perf_counter()
    completed = False
    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=bob["peer_id"])
    start_t_ms = max(t_ms, 5000)

    for i in range(args.max_rounds):
        t_ms = start_t_ms + i * tick_helper.TICK_INTERVAL_MS
        _run_jobs(t_ms, db, job_names)

        row = safedb.query_one(
            "SELECT COUNT(*) as count FROM file_slices WHERE file_id = ? AND recorded_by = ?",
            (file_id, bob["peer_id"]),
        )
        if i % 200 == 0:
            status = negentropy.get_all_connection_sync_status(db, bob["peer_id"], t_ms)
            count = row["count"] if row else 0
            unsafedb = create_unsafe_db(db)
            safedb = create_safe_db(db, recorded_by=bob["peer_id"])
            conn_row = safedb.query_one(
                "SELECT key_id FROM connections WHERE recorded_by = ? AND their_key IS NOT NULL LIMIT 1",
                (bob["peer_id"],),
            )
            conn_id = conn_row["key_id"] if conn_row else None
            conn_ready = False
            if conn_id:
                ready_row = safedb.query_one(
                    "SELECT their_key_id, their_key, peer_shared_id, invite_id FROM connections "
                    "WHERE recorded_by = ? AND key_id = ?",
                    (bob["peer_id"], conn_id),
                )
                conn_ready = bool(ready_row and ready_row["their_key"])
                conn_peer_shared = ready_row["peer_shared_id"] if ready_row else None
                conn_invite = ready_row["invite_id"] if ready_row else None
            status_counts = {}
            pending_sample = []
            if conn_id:
                rows = safedb.query(
                    "SELECT status, COUNT(*) as cnt FROM negentropy_sync_state "
                    "WHERE recorded_by = ? AND connection_id = ? GROUP BY status",
                    (bob["peer_id"], conn_id),
                )
                status_counts = {r["status"]: r["cnt"] for r in rows}
                pending_sample = safedb.query(
                    "SELECT range_id, level, prefix, status FROM negentropy_sync_state "
                    "WHERE recorded_by = ? AND connection_id = ? AND status = 'pending' LIMIT 5",
                    (bob["peer_id"], conn_id),
                )
            sync_cursor = unsafedb.query_one(
                "SELECT last_log_id FROM projection_streams WHERE stream_name = ?",
                ("sync_update",),
            )
            log_tail = unsafedb.query_one("SELECT MAX(id) as max_id FROM incoming_event_log")
            incoming = unsafedb.query_one(
                "SELECT COUNT(*) as cnt FROM incoming_event_log WHERE recorded_by = ?",
                (bob["peer_id"],),
            )
            sample = unsafedb.query_one(
                "SELECT blob FROM incoming_event_log WHERE recorded_by = ? ORDER BY id DESC LIMIT 1",
                (bob["peer_id"],),
            )
            sample_type = "none"
            if sample and sample["blob"]:
                blob = sample["blob"]
                if wire_format.is_wire_file_slice(blob):
                    sample_type = "file_slice"
                elif wire_format.is_wire_message_attachment_envelope(blob):
                    sample_type = "message_attachment"
                elif wire_format.is_wire_negentropy_envelope(blob):
                    sample_type = "negentropy"
                else:
                    sample_type = "unknown"
            neg_count = safedb.query_one(
                "SELECT COUNT(*) as cnt FROM negentropy_events WHERE recorded_by = ?",
                (bob["peer_id"],),
            )
            shareable = safedb.query_one(
                "SELECT COUNT(*) as cnt FROM shareable_events WHERE can_share_peer_id = ?",
                (bob["peer_id"],),
            )
            alice_db = create_safe_db(db, recorded_by=alice["peer_id"])
            alice_neg = alice_db.query_one(
                "SELECT COUNT(*) as cnt FROM negentropy_events WHERE recorded_by = ?",
                (alice["peer_id"],),
            )
            alice_shareable = alice_db.query_one(
                "SELECT COUNT(*) as cnt FROM shareable_events WHERE can_share_peer_id = ?",
                (alice["peer_id"],),
            )
            print(
                f"[round {i}] slices={count}/{total_slices} "
                f"synced={status['all_synced']} progress={status['connections'][0]['progress_pct'] if status['connections'] else 0:.1f}% "
                f"neg={neg_count['cnt'] if neg_count else 0} shareable={shareable['cnt'] if shareable else 0} "
                f"alice_neg={alice_neg['cnt'] if alice_neg else 0} alice_shareable={alice_shareable['cnt'] if alice_shareable else 0} "
                f"incoming={incoming['cnt'] if incoming else 0} "
                f"sync_cursor={sync_cursor['last_log_id'] if sync_cursor else 0} log_tail={log_tail['max_id'] if log_tail else 0} "
                f"sample={sample_type} conn_ready={conn_ready} peer_shared={bool(conn_peer_shared)} invite={bool(conn_invite)} "
                f"statuses={status_counts} "
                f"pending_sample={pending_sample}"
            )
        if row and row["count"] >= total_slices:
            completed = True
            break

    elapsed = time.perf_counter() - start
    mib = (args.size_mb * 1024 * 1024) / (1024 * 1024)
    mib_per_sec = mib / elapsed if elapsed > 0 else 0.0
    print(f"Sync loop complete in {elapsed:.2f}s, throughput ~{mib_per_sec:.2f} MiB/s")

    if not completed:
        raise SystemExit("sync loop did not complete within max rounds")


if __name__ == "__main__":
    main()
