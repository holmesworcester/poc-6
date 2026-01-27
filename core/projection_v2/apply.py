"""Apply layer for projection v2."""
from __future__ import annotations

import logging
from typing import Any

from core.db import SUBJECTIVE_TABLES, create_safe_db, create_unsafe_db
from .types import ProjectorResult, EmitEvent, Command

log = logging.getLogger(__name__)

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

    # Handle emitted events
    if result.emit_events:
        _apply_emit_events(result.emit_events, recorded_by, recorded_at, db)

    # Execute commands (side effects)
    if result.commands:
        _execute_commands(result.commands, recorded_by, recorded_at, db)


def _apply_emit_events(
    emit_events: tuple[EmitEvent, ...],
    recorded_by: str,
    recorded_at: int,
    db: Any
) -> None:
    """Create and project emitted events.

    Emitted events are deterministic - the projector computed the event_data,
    we just need to store and project them.
    """
    from core import store
    from core import crypto
    from core import wire_format
    from events.identity import peer

    for emit in emit_events:
        peer_id = emit.peer_id or recorded_by

        # Get the event module for this type to check if it needs signing
        from events import registry
        event_spec = registry.get_event_spec(emit.event_type)

        event_data = dict(emit.event_data)
        event_data['type'] = emit.event_type

        # Sign if needed (most events need signing)
        if event_spec and event_spec.get('signer'):
            private_key = peer.get_private_key(peer_id, peer_id, db)
            event_data = crypto.sign_event(event_data, private_key)

        # Store the event
        if emit.event_type == "group_key":
            key_b64 = event_data.get("key")
            if not key_b64:
                raise ValueError("group_key emit missing key material")
            blob = wire_format.encode_group_key_wire_event(
                key=crypto.b64decode(key_b64),
                created_at_ms=0,
            )
        else:
            blob = crypto.canonicalize_json(event_data)
        event_id = store.event(blob, peer_id, recorded_at, db)

        log.debug(f"apply: emitted {emit.event_type} event {event_id[:20]}...")


def cascade_delete_from_valid_events(event_id: str, recorded_by: str, db: Any) -> int:
    """Cascade delete an event and all dependents from valid_events.

    When an event is deleted/invalidated, all events that depend on it
    must also be removed from valid_events for convergence.

    Args:
        event_id: The event being deleted
        recorded_by: Peer scope
        db: Database connection

    Returns:
        Total number of events deleted (including dependents)
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    deleted_count = 0
    visited = set()

    def _cascade(eid: str) -> int:
        if eid in visited:
            return 0
        visited.add(eid)
        count = 0

        # Find all children (events that depend on this one)
        children = safedb.query(
            """SELECT DISTINCT child_event_id
               FROM event_dependencies
               WHERE parent_event_id = ? AND recorded_by = ?""",
            (eid, recorded_by)
        )

        # Recursively delete children first
        for child in children:
            count += _cascade(child['child_event_id'])

        # Delete this event from valid_events
        safedb.execute(
            "DELETE FROM valid_events WHERE event_id = ? AND recorded_by = ?",
            (eid, recorded_by)
        )
        return count + 1

    deleted_count = _cascade(event_id)
    log.debug(f"cascade_delete: removed {deleted_count} events from valid_events for {event_id[:20]}...")
    return deleted_count


# ============================================================================
# Command execution
# ============================================================================

# Registry of command handlers
# Each handler takes (args, recorded_by, recorded_at, db)
_COMMAND_HANDLERS: dict[str, Any] = {}


def register_command_handler(command_type: str, handler: Any) -> None:
    """Register a handler for a command type.

    Args:
        command_type: The command type string (e.g., 'send_connection_ack')
        handler: Function taking (args, recorded_by, recorded_at, db)
    """
    _COMMAND_HANDLERS[command_type] = handler
    log.debug(f"Registered command handler: {command_type}")


def _execute_commands(
    commands: tuple[Command, ...],
    recorded_by: str,
    recorded_at: int,
    db: Any
) -> None:
    """Execute commands (side effects) after projection.

    Commands are non-deterministic operations that projectors need
    but cannot execute themselves (since project_pure must be pure).
    """
    for cmd in commands:
        handler = _COMMAND_HANDLERS.get(cmd.command_type)
        if handler:
            try:
                handler(cmd.args, recorded_by, recorded_at, db)
                log.debug(f"Executed command: {cmd.command_type}")
            except Exception as e:
                log.warning(f"Command {cmd.command_type} failed: {e}")
        else:
            log.warning(f"No handler for command: {cmd.command_type}")
