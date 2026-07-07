"""
Perf prototype: SQLite-only log table vs log+index variants.

Run with:
  SYNC_PERF_APPEND_LOG=1 SYNC_PERF_APPEND_LOG_COUNT=200000 \
    SYNC_PERF_APPEND_LOG_SMALL_SIZE=512 SYNC_PERF_APPEND_LOG_FILE_SIZE=800 \
    SYNC_PERF_APPEND_LOG_FILE_RATIO=10 SYNC_PERF_APPEND_LOG_BATCH=200 \
    SYNC_PERF_APPEND_LOG_RANDOM_IDS=1 \
    pytest -k perf_append_log -s
"""
from __future__ import annotations

import os
import random
import sqlite3
import time
import uuid
from pathlib import Path

import pytest


_DISK_TEST_DIR = Path(__file__).parent.parent / ".test_dbs"


def _make_rows_mixed(
    start: int,
    count: int,
    small_size: int,
    file_size: int,
    file_ratio: int,
    random_ids: bool,
    include_shareable: bool,
) -> tuple[list[tuple[int, bytes, bytes, int]], list[tuple[bytes, int]]]:
    if small_size < 8 or file_size < 8:
        raise ValueError("blob size must be >= 8 bytes")
    if file_ratio < 1:
        raise ValueError("file_ratio must be >= 1")
    rows = []
    shareable_rows = []
    small_buf = bytearray(small_size)
    file_buf = bytearray(file_size)
    stride = file_ratio + 1
    for i in range(start, start + count):
        is_small = (i - start) % stride == 0
        buf = small_buf if is_small else file_buf
        buf[:8] = i.to_bytes(8, "little")
        if random_ids:
            event_id = random.getrandbits(128).to_bytes(16, "little")
        else:
            event_id = i.to_bytes(16, "little")
        rows.append((i, event_id, bytes(buf), 1 if is_small else 0))
        if include_shareable and is_small:
            shareable_rows.append((event_id, i))
    return rows, shareable_rows


def _run_sqlite_log(
    db_path: Path,
    count: int,
    small_size: int,
    file_size: int,
    file_ratio: int,
    do_fsync: bool,
    index_mode: str,
    batch_size: int,
    random_ids: bool,
) -> float:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA synchronous = {'FULL' if do_fsync else 'OFF'}")
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute("PRAGMA cache_size = -4096")
    conn.execute("PRAGMA mmap_size = 0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY,
            event_id BLOB NOT NULL,
            data BLOB NOT NULL,
            is_small INTEGER NOT NULL
        )
    """)
    if index_mode == "all":
        conn.execute("CREATE INDEX IF NOT EXISTS event_log_event_id_idx ON event_log (event_id)")
    elif index_mode == "shareable":
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shareable_index (
                event_id BLOB PRIMARY KEY,
                log_id INTEGER NOT NULL
            )
        """)
    conn.commit()

    start = time.perf_counter()
    conn.execute("BEGIN")
    remaining = count
    cursor = 0
    while remaining > 0:
        batch = min(batch_size, remaining)
        rows, shareable_rows = _make_rows_mixed(
            cursor + 1,
            batch,
            small_size,
            file_size,
            file_ratio,
            random_ids,
            index_mode == "shareable",
        )
        conn.executemany(
            "INSERT INTO event_log (id, event_id, data, is_small) VALUES (?, ?, ?, ?)",
            rows,
        )
        if index_mode == "shareable" and shareable_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO shareable_index (event_id, log_id) VALUES (?, ?)",
                shareable_rows,
            )
        cursor += batch
        remaining -= batch
    conn.commit()
    conn.close()
    return time.perf_counter() - start


def test_perf_append_log() -> None:
    if os.getenv("SYNC_PERF_APPEND_LOG") != "1":
        pytest.skip("Set SYNC_PERF_APPEND_LOG=1 to run")

    count = int(os.getenv("SYNC_PERF_APPEND_LOG_COUNT", "200000"))
    small_size = int(os.getenv("SYNC_PERF_APPEND_LOG_SMALL_SIZE", "512"))
    file_size = int(os.getenv("SYNC_PERF_APPEND_LOG_FILE_SIZE", "800"))
    file_ratio = int(os.getenv("SYNC_PERF_APPEND_LOG_FILE_RATIO", "10"))
    do_fsync = os.getenv("SYNC_PERF_APPEND_LOG_FSYNC") == "1"
    batch_size = int(os.getenv("SYNC_PERF_APPEND_LOG_BATCH", "200"))
    random_ids = os.getenv("SYNC_PERF_APPEND_LOG_RANDOM_IDS", "1") != "0"

    _DISK_TEST_DIR.mkdir(exist_ok=True)
    log_path = _DISK_TEST_DIR / f"sqlite_log_{uuid.uuid4().hex}.db"
    idx_path = _DISK_TEST_DIR / f"sqlite_log_{uuid.uuid4().hex}.db"

    t_log = _run_sqlite_log(
        log_path,
        count,
        small_size,
        file_size,
        file_ratio,
        do_fsync,
        "none",
        batch_size,
        random_ids,
    )
    t_idx = _run_sqlite_log(
        idx_path,
        count,
        small_size,
        file_size,
        file_ratio,
        do_fsync,
        "all",
        batch_size,
        random_ids,
    )
    t_shareable = _run_sqlite_log(
        _DISK_TEST_DIR / f"sqlite_log_{uuid.uuid4().hex}.db",
        count,
        small_size,
        file_size,
        file_ratio,
        do_fsync,
        "shareable",
        batch_size,
        random_ids,
    )

    avg_size = (file_ratio * file_size + small_size) / (file_ratio + 1)
    mb = (count * avg_size) / (1024 * 1024)
    print(
        f"mix: {file_ratio}:1 files:small "
        f"(file={file_size}B, small={small_size}B, avg={avg_size:.1f}B)"
    )
    print(f"event_id: {'random' if random_ids else 'sequential'}")
    print(
        f"sqlite log-only: {count} blobs in {t_log:.3f}s "
        f"({count / t_log:.1f} blobs/s, {mb / t_log:.1f} MB/s)"
    )
    print(
        f"sqlite log+index: {count} blobs in {t_idx:.3f}s "
        f"({count / t_idx:.1f} blobs/s, {mb / t_idx:.1f} MB/s)"
    )
    print(
        f"sqlite log+shareable-index: {count} blobs in {t_shareable:.3f}s "
        f"({count / t_shareable:.1f} blobs/s, {mb / t_shareable:.1f} MB/s)"
    )

    assert t_log > 0 and t_idx > 0 and t_shareable > 0
