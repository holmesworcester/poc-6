"""SQLite ceiling microbench for file-slice style writes.

Run with:
  PYTHONPATH=. python3 tests/perf_file_slice_sqlite_ceiling.py
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time

from core import crypto, schema, wire_format
from core.db import Database
from events.network import negentropy


def _apply_perf_tuning(fast: bool) -> None:
    if not fast:
        return
    os.environ.setdefault("SQLITE_SYNCHRONOUS", "NORMAL")
    os.environ.setdefault("SQLITE_TEMP_STORE", "MEMORY")
    os.environ.setdefault("SQLITE_CACHE_SIZE", "-131072")  # ~128MB
    os.environ.setdefault("SQLITE_WAL_AUTOCHECKPOINT", "1000")


def _create_db(db_path: str | None) -> Database:
    conn = sqlite3.Connection(db_path or ":memory:")
    db = Database(conn)
    schema.create_all(db)
    return db


def _build_payloads(num_slices: int) -> dict[str, list]:
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


def _bench_variant(name: str, fn) -> tuple[str, float]:
    start = time.perf_counter()
    fn()
    elapsed = time.perf_counter() - start
    return name, elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slices", type=int, default=25000)
    parser.add_argument("--db", type=str, default="")
    parser.add_argument("--fast", action="store_true")
    args = parser.parse_args()

    _apply_perf_tuning(args.fast)
    payloads = _build_payloads(args.slices)

    variants: list[tuple[str, float]] = []

    def run_store_only() -> None:
        db = _create_db(args.db or None)
        db._conn.executemany(
            "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
            payloads["event_rows"],
        )
        db.commit()

    def run_file_slices_only() -> None:
        db = _create_db(args.db or None)
        db._conn.executemany(
            """INSERT OR IGNORE INTO file_slices
               (file_id, slice_number, nonce, ciphertext, poly_tag, event_id, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            payloads["file_slice_rows"],
        )
        db.commit()

    def run_full_without_negentropy() -> None:
        db = _create_db(args.db or None)
        db._conn.executemany(
            "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
            payloads["event_rows"],
        )
        db._conn.executemany(
            "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
            payloads["recorded_rows"],
        )
        db._conn.executemany(
            "INSERT OR IGNORE INTO ingest_index "
            "(log_id, event_id, recorded_id, recorded_by, hint, event_type, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payloads["ingest_rows"],
        )
        db._conn.executemany(
            """INSERT OR IGNORE INTO file_slices
               (file_id, slice_number, nonce, ciphertext, poly_tag, event_id, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            payloads["file_slice_rows"],
        )
        db._conn.executemany(
            """INSERT OR IGNORE INTO event_dependencies
               (child_event_id, parent_event_id, recorded_by, dependency_type)
               VALUES (?, ?, ?, ?)""",
            payloads["dep_rows"],
        )
        db.commit()

    def run_full_with_negentropy() -> None:
        db = _create_db(args.db or None)
        db._conn.executemany(
            "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
            payloads["event_rows"],
        )
        db._conn.executemany(
            "INSERT OR IGNORE INTO store (id, blob, stored_at) VALUES (?, ?, ?)",
            payloads["recorded_rows"],
        )
        db._conn.executemany(
            "INSERT OR IGNORE INTO ingest_index "
            "(log_id, event_id, recorded_id, recorded_by, hint, event_type, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payloads["ingest_rows"],
        )
        db._conn.executemany(
            """INSERT OR IGNORE INTO file_slices
               (file_id, slice_number, nonce, ciphertext, poly_tag, event_id, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            payloads["file_slice_rows"],
        )
        db._conn.executemany(
            """INSERT OR IGNORE INTO event_dependencies
               (child_event_id, parent_event_id, recorded_by, dependency_type)
               VALUES (?, ?, ?, ?)""",
            payloads["dep_rows"],
        )
        negentropy.add_shareable_events_batch(
            payloads["shareable_batch"],
            payloads["recorded_by"],
            db,
            defer_buckets=False,
        )
        db.commit()

    variants.append(_bench_variant("store_only", run_store_only))
    variants.append(_bench_variant("file_slices_only", run_file_slices_only))
    variants.append(_bench_variant("full_no_negentropy", run_full_without_negentropy))
    variants.append(_bench_variant("full_with_negentropy", run_full_with_negentropy))

    payload_mib = payloads["payload_bytes"] / (1024 * 1024)
    wire_mib = payloads["wire_bytes"] / (1024 * 1024)
    slice_count = payloads["slice_count"]

    print(f"Slices: {slice_count}")
    print(f"Payload MiB: {payload_mib:.2f} (ciphertext only)")
    print(f"Wire MiB: {wire_mib:.2f} (full 512B event)")
    print("")

    for name, elapsed in variants:
        rows_per_sec = slice_count / elapsed if elapsed > 0 else 0.0
        payload_mib_s = payload_mib / elapsed if elapsed > 0 else 0.0
        wire_mib_s = wire_mib / elapsed if elapsed > 0 else 0.0
        print(f"{name:22s}  {elapsed:6.2f}s  {rows_per_sec:10.0f} rows/s  "
              f"{payload_mib_s:7.2f} MiB/s payload  {wire_mib_s:7.2f} MiB/s wire")


if __name__ == "__main__":
    main()
