"""Priority projection helpers.

Priority selection is based on ingest_index.event_type.
"""
from __future__ import annotations

from typing import Any
import time

from core import ingest


def project_stream(
    db: Any,
    stream_name: str,
    include_types: list[str] | None,
    exclude_types: list[str] | None,
    batch_size: int,
    budget_ms: int,
    t_ms: int,
    project_fn,
) -> int:
    """Project events for a stream, respecting a time budget."""
    if batch_size < 1 or budget_ms < 1:
        return 0

    last_log_id = ingest.get_stream_cursor(db, stream_name)
    start = time.perf_counter()
    processed = 0

    while (time.perf_counter() - start) * 1000 < budget_ms:
        rows = _fetch_ingest_rows(db, last_log_id, include_types, exclude_types, batch_size)
        if not rows:
            break

        recorded_ids = [row[0] for row in rows]
        max_log_id = rows[-1][1]
        project_fn(recorded_ids)

        ingest.set_stream_cursor(db, stream_name, max_log_id, t_ms)
        last_log_id = max_log_id
        processed += len(rows)

    return processed


def _fetch_ingest_rows(
    db: Any,
    last_log_id: int,
    include_types: list[str] | None,
    exclude_types: list[str] | None,
    limit: int,
) -> list[tuple[str, int]]:
    """Fetch (recorded_id, log_id) rows from ingest_index."""
    if include_types:
        placeholders = ",".join(["?"] * len(include_types))
        params = (last_log_id, *include_types, limit)
        rows = db._conn.execute(
            f"SELECT recorded_id, log_id FROM ingest_index "
            f"WHERE log_id > ? AND event_type IN ({placeholders}) "
            f"ORDER BY log_id LIMIT ?",
            params,
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    if exclude_types:
        placeholders = ",".join(["?"] * len(exclude_types))
        params = (last_log_id, *exclude_types, limit)
        rows = db._conn.execute(
            f"SELECT recorded_id, log_id FROM ingest_index "
            f"WHERE log_id > ? AND (event_type IS NULL OR event_type NOT IN ({placeholders})) "
            f"ORDER BY log_id LIMIT ?",
            params,
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    rows = db._conn.execute(
        "SELECT recorded_id, log_id FROM ingest_index "
        "WHERE log_id > ? ORDER BY log_id LIMIT ?",
        (last_log_id, limit),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]
