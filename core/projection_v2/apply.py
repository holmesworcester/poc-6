"""Apply layer for projection v2."""
from __future__ import annotations

from typing import Any

from core.db import SUBJECTIVE_TABLES, create_safe_db, create_unsafe_db
from .types import ProjectorResult

_SCOPE_FIELD_BY_TABLE = {
    "shareable_events": "can_share_peer_id",
}


def apply_writes(result: ProjectorResult, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Apply ProjectorResult writes to the database.

    Treats valid_event=False as a reject (no writes).
    """
    if not result.valid_event:
        return

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    for write in result.writes:
        table = write.table
        values = dict(write.values or {})
        where = dict(write.where) if write.where else None
        use_safe = table in SUBJECTIVE_TABLES
        scope_field = _SCOPE_FIELD_BY_TABLE.get(table, "recorded_by") if use_safe else None

        if use_safe:
            if write.op == "insert":
                if scope_field not in values:
                    values[scope_field] = recorded_by
                elif values[scope_field] != recorded_by:
                    raise ValueError(f"{table} insert scope mismatch for {scope_field}")
            else:
                if where is None:
                    where = {}
                if scope_field not in where:
                    where[scope_field] = recorded_by
                elif where[scope_field] != recorded_by:
                    raise ValueError(f"{table} write scope mismatch for {scope_field}")

        target_db = safedb if use_safe else unsafedb

        if write.op == "insert":
            columns = list(values.keys())
            if not columns:
                raise ValueError(f"{table} insert requires values")
            placeholders = ", ".join(["?"] * len(columns))
            sql = f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            params = tuple(values[col] for col in columns)
            target_db.execute(sql, params)
        elif write.op == "update":
            if where is None:
                raise ValueError(f"{table} update requires where clause")
            set_columns = list(values.keys())
            if not set_columns:
                raise ValueError(f"{table} update requires values")
            set_clause = ", ".join(f"{col} = ?" for col in set_columns)
            where_columns = list(where.keys())
            where_clause = " AND ".join(f"{col} = ?" for col in where_columns)
            sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
            params = tuple(values[col] for col in set_columns) + tuple(
                where[col] for col in where_columns
            )
            target_db.execute(sql, params)
        elif write.op == "delete":
            if where is None:
                raise ValueError(f"{table} delete requires where clause")
            where_columns = list(where.keys())
            where_clause = " AND ".join(f"{col} = ?" for col in where_columns)
            sql = f"DELETE FROM {table} WHERE {where_clause}"
            params = tuple(where[col] for col in where_columns)
            target_db.execute(sql, params)
        else:
            raise ValueError(f"Unknown write op: {write.op}")
