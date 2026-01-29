"""Helpers for projection v2 testing.

Provides utilities for:
- Building minimal DB state for isolated tests
- Comparing legacy project() vs v2 project_pure() outcomes
- Creating test events with controlled signer_type
"""
from typing import Any
from core import crypto
from core import store
from core.db import create_safe_db
from events.identity import admin as admin_module


def build_signed_event(
    event_type: str,
    event_data: dict[str, Any],
    private_key: bytes,
    signer_type: str | None = None,
    signed_by: str | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Build a signed event blob with optional signer_type field.

    Args:
        event_type: Event type string
        event_data: Base event data (will have type added)
        private_key: Key to sign with
        signer_type: If provided, adds signer_type field (required for v2)
        signed_by: If provided, adds signed_by field

    Returns:
        tuple: (blob bytes, full event data dict including signature)
    """
    full_data = {'type': event_type, **event_data}

    if signer_type is not None:
        full_data['signer_type'] = signer_type
    if signed_by is not None:
        full_data['signed_by'] = signed_by

    signed_bytes = b"test-wire-payload"
    signature = crypto.sign(signed_bytes, private_key)
    full_data["_wire_signed_bytes"] = signed_bytes
    full_data["_wire_signature"] = signature

    # Build a minimal wire envelope if possible; fall back to empty blob for resolver-only tests.
    blob = admin_module.encode_wire_event(
        user_id_b64=full_data.get("user_id", crypto.b64encode(b"\x00" * 32)),
        network_id_b64=full_data.get("network_id", crypto.b64encode(b"\x00" * 32)),
        signed_by_b64=full_data.get("signed_by", crypto.b64encode(b"\x00" * 32)),
        signer_type=full_data.get("signer_type", "network"),
        admin_grant_id_b64=full_data.get("admin_grant"),
        created_at_ms=full_data.get("created_at", 0),
        private_key=private_key,
    ) if event_type == "admin" else b""

    return blob, full_data


def store_and_record_event(
    blob: bytes,
    peer_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Store event blob and create recorded wrapper.

    This is equivalent to store.event() but returns just the event_id.
    """
    return store.event(blob, peer_id, t_ms, db)


def get_table_rows(
    table: str,
    recorded_by: str,
    db: Any,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Query all rows from a table for a given recorded_by.

    Args:
        table: Table name
        recorded_by: Peer perspective
        db: Database connection
        where: Optional additional WHERE conditions

    Returns:
        List of row dicts
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    query = f"SELECT * FROM {table} WHERE recorded_by = ?"
    params: list[Any] = [recorded_by]

    if where:
        for col, val in where.items():
            query += f" AND {col} = ?"
            params.append(val)

    return list(safedb.query(query, tuple(params)))


def compare_table_state(
    table: str,
    recorded_by: str,
    db: Any,
    expected_rows: list[dict[str, Any]],
    key_columns: list[str] | None = None,
) -> tuple[bool, str]:
    """Compare actual table state against expected rows.

    Args:
        table: Table name
        recorded_by: Peer perspective
        db: Database connection
        expected_rows: Expected row dicts (subset of columns OK)
        key_columns: Columns to use for matching (default: all expected columns)

    Returns:
        tuple: (matches: bool, diff_message: str)
    """
    actual_rows = get_table_rows(table, recorded_by, db)

    if not expected_rows and not actual_rows:
        return True, "Both empty"

    if len(expected_rows) != len(actual_rows):
        return False, f"Row count mismatch: expected {len(expected_rows)}, got {len(actual_rows)}"

    # Compare each expected row against actual
    for expected in expected_rows:
        cols = key_columns or list(expected.keys())
        found = False

        for actual in actual_rows:
            if all(actual.get(col) == expected.get(col) for col in cols if col in expected):
                found = True
                break

        if not found:
            return False, f"Expected row not found: {expected}"

    return True, "Match"


def check_valid_event(event_id: str, recorded_by: str, db: Any) -> bool:
    """Check if an event is marked as valid for a peer."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (event_id, recorded_by)
    )
    return row is not None


def check_blocked_event(recorded_id: str, recorded_by: str, db: Any) -> bool:
    """Check if a recorded event is in the blocked queue."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT 1 FROM blocked_events_ephemeral WHERE recorded_id = ? AND recorded_by = ?",
        (recorded_id, recorded_by)
    )
    return row is not None


def get_blocked_deps(recorded_id: str, recorded_by: str, db: Any) -> list[str]:
    """Get list of missing deps for a blocked event."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    rows = safedb.query(
        "SELECT dep_event_id FROM blocked_event_deps_ephemeral WHERE recorded_id = ? AND recorded_by = ?",
        (recorded_id, recorded_by)
    )
    return [row['dep_event_id'] for row in rows]
