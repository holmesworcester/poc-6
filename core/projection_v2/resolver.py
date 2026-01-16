"""Resolver for projection v2."""
from __future__ import annotations

from typing import Any

from core import crypto, store
from core.db import SUBJECTIVE_TABLES, create_safe_db, create_unsafe_db
from events import registry
from .types import ProjectionContext, ResolveResult

_CONTEXT_KEYS = {
    "@recorded_by",
    "@recorded_at",
    "@event_id",
    "@event_type",
}


def _is_event_valid(event_id: str, recorded_by: str, safedb: Any) -> bool:
    row = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
        (event_id, recorded_by),
    )
    return row is not None


def _context_value(
    key_from: str | None,
    ref_id: str,
    event_type: str,
    event_data: dict[str, Any],
    recorded_by: str,
    recorded_at: int,
) -> Any:
    if not key_from:
        return None
    if not key_from.startswith("@"):
        key_from = f"@{key_from}"
    if key_from not in _CONTEXT_KEYS:
        return None
    if key_from == "@recorded_by":
        return recorded_by
    if key_from == "@recorded_at":
        return recorded_at
    if key_from == "@event_id":
        return ref_id
    if key_from == "@event_type":
        return event_type
    return None


def _field_looks_like_pubkey(field_name: str | None) -> bool:
    if not field_name:
        return False
    lowered = field_name.lower()
    return "pubkey" in lowered or "public_key" in lowered


def _fetch_dep_row(
    table: str,
    fields: list[str] | None,
    key_field: str,
    key_value: Any,
    recorded_by: str,
    safedb: Any,
    unsafedb: Any,
) -> dict[str, Any] | None:
    if fields:
        columns = ", ".join(fields)
    else:
        columns = "*"
    where_clauses = [f"{key_field} = ?"]
    params: list[Any] = [key_value]
    use_safe = table in SUBJECTIVE_TABLES
    if use_safe and key_field not in ("recorded_by", "can_share_peer_id"):
        where_clauses.append("recorded_by = ?")
        params.append(recorded_by)
    sql = f"SELECT {columns} FROM {table} WHERE {' AND '.join(where_clauses)}"
    db = safedb if use_safe else unsafedb
    return db.query_one(sql, tuple(params))


def _resolve_table_dep(
    dep_name: str,
    dep_spec: dict[str, Any],
    event_data: dict[str, Any],
    recorded_by: str,
    safedb: Any,
    unsafedb: Any,
    required: bool,
) -> tuple[str, Any | None, list[str], str | None]:
    key_field = dep_spec.get("key")
    if not key_field:
        return "reject", None, [], f"dep '{dep_name}' missing key"
    value_field = dep_spec.get("key_from") or key_field
    if value_field not in event_data or event_data.get(value_field) in (None, ""):
        if required:
            return "reject", None, [], f"dep '{dep_name}' missing value for {value_field}"
        return "ok", None, [], None
    dep_id = event_data.get(value_field)
    if not isinstance(dep_id, str):
        if required:
            return "reject", None, [], f"dep '{dep_name}' invalid id type"
        return "ok", None, [], None
    if not _is_event_valid(dep_id, recorded_by, safedb):
        if required:
            return "block", None, [dep_id], None
        return "ok", None, [], None
    table = dep_spec.get("table")
    if not table:
        return "reject", None, [], f"dep '{dep_name}' missing table"
    fields = dep_spec.get("fields")
    row = _fetch_dep_row(table, fields, key_field, dep_id, recorded_by, safedb, unsafedb)
    if not row:
        if required:
            return "reject", None, [], f"dep '{dep_name}' missing row in {table}"
        return "ok", None, [], None
    return "ok", row, [], None


def _resolve_value_dep(
    dep_name: str,
    dep_spec: dict[str, Any],
    event_data: dict[str, Any],
    required: bool,
) -> tuple[str, Any | None, list[str], str | None]:
    if "value" in dep_spec:
        return "ok", dep_spec.get("value"), [], None
    key_field = dep_spec.get("key")
    if not key_field:
        if required:
            return "reject", None, [], f"dep '{dep_name}' missing key"
        return "ok", None, [], None
    if key_field not in event_data:
        if required:
            return "reject", None, [], f"dep '{dep_name}' missing value for {key_field}"
        return "ok", None, [], None
    value = event_data.get(key_field)
    if value is None and required:
        return "reject", None, [], f"dep '{dep_name}' missing value for {key_field}"
    return "ok", value, [], None


def _resolve_context_dep(
    dep_name: str,
    dep_spec: dict[str, Any],
    ref_id: str,
    event_type: str,
    event_data: dict[str, Any],
    recorded_by: str,
    recorded_at: int,
    safedb: Any,
    unsafedb: Any,
    required: bool,
) -> tuple[str, Any | None, list[str], str | None]:
    lookup_value = _context_value(
        dep_spec.get("key_from"),
        ref_id,
        event_type,
        event_data,
        recorded_by,
        recorded_at,
    )
    if lookup_value is None:
        if required:
            return "reject", None, [], f"dep '{dep_name}' missing context value"
        return "ok", None, [], None
    table = dep_spec.get("table")
    if not table:
        return "ok", lookup_value, [], None
    key_field = dep_spec.get("key")
    if not key_field:
        if dep_spec.get("key_from") in ("@recorded_by", "recorded_by"):
            key_field = "recorded_by"
        else:
            return "reject", None, [], f"dep '{dep_name}' missing key"
    row = _fetch_dep_row(table, dep_spec.get("fields"), key_field, lookup_value, recorded_by, safedb, unsafedb)
    if not row:
        if required:
            if isinstance(lookup_value, str):
                return "block", None, [lookup_value], None
            return "reject", None, [], f"dep '{dep_name}' missing row in {table}"
        return "ok", None, [], None
    return "ok", row, [], None


def _resolve_dep(
    dep_name: str,
    dep_spec: dict[str, Any],
    ref_id: str,
    event_type: str,
    event_data: dict[str, Any],
    recorded_by: str,
    recorded_at: int,
    safedb: Any,
    unsafedb: Any,
    required: bool,
) -> tuple[str, Any | None, list[str], str | None]:
    source = dep_spec.get("source")
    if source == "table":
        return _resolve_table_dep(
            dep_name, dep_spec, event_data, recorded_by, safedb, unsafedb, required
        )
    if source == "value":
        return _resolve_value_dep(dep_name, dep_spec, event_data, required)
    if source == "context":
        return _resolve_context_dep(
            dep_name,
            dep_spec,
            ref_id,
            event_type,
            event_data,
            recorded_by,
            recorded_at,
            safedb,
            unsafedb,
            required,
        )
    return "reject", None, [], f"dep '{dep_name}' has unknown source"


def _resolve_invite_pubkey(
    invite_id: str,
    recorded_by: str,
    safedb: Any,
    unsafedb: Any,
) -> bytes | None:
    row = safedb.query_one(
        "SELECT invite_pubkey FROM invites WHERE invite_id = ? AND recorded_by = ? LIMIT 1",
        (invite_id, recorded_by),
    )
    if row and row.get("invite_pubkey"):
        return crypto.b64decode(row["invite_pubkey"])
    blob = store.get(invite_id, unsafedb)
    if not blob:
        return None
    try:
        invite_data = crypto.parse_json(blob)
    except Exception:
        return None
    invite_pubkey = invite_data.get("invite_pubkey")
    if not invite_pubkey:
        return None
    return crypto.b64decode(invite_pubkey)


def _resolve_signer(
    event_spec: dict[str, Any],
    ref_id: str,
    event_type: str,
    event_data: dict[str, Any],
    recorded_by: str,
    recorded_at: int,
    db: Any,
    safedb: Any,
    unsafedb: Any,
) -> tuple[str, dict[str, Any] | None, list[str], str | None]:
    signer_spec = event_spec.get("signer")
    if not signer_spec:
        return "ok", None, [], None
    type_field = signer_spec.get("type_field")
    if not type_field:
        return "reject", None, [], "signer type_field required"
    signer_type = event_data.get(type_field)
    if not signer_type:
        return "reject", None, [], "signer_type missing"
    id_field = signer_spec.get("id_field")
    if not id_field:
        return "reject", None, [], "signer id_field required"
    signer_id = event_data.get(id_field)
    if not signer_id:
        return "reject", None, [], "signer id missing"

    public_key: bytes | None = None

    if signer_type == "peer_shared":
        if not _is_event_valid(signer_id, recorded_by, safedb):
            return "block", None, [signer_id], None
        from events.identity import peer_shared

        try:
            public_key = peer_shared.get_public_key(signer_id, recorded_by, db)
        except ValueError:
            return "reject", None, [], "peer_shared signer not available"
    elif signer_type == "invite":
        if not _is_event_valid(signer_id, recorded_by, safedb):
            return "block", None, [signer_id], None
        public_key = _resolve_invite_pubkey(signer_id, recorded_by, safedb, unsafedb)
        if not public_key:
            return "reject", None, [], "invite signer not available"
    elif signer_type == "network":
        if _field_looks_like_pubkey(id_field):
            try:
                public_key = crypto.b64decode(signer_id)
            except Exception:
                return "reject", None, [], "invalid network pubkey"
        else:
            if not _is_event_valid(signer_id, recorded_by, safedb):
                return "block", None, [signer_id], None
            from events.identity import network

            try:
                public_key = network.get_public_key(signer_id, recorded_by, db)
            except ValueError:
                return "reject", None, [], "network signer not available"
    else:
        return "reject", None, [], f"unknown signer type '{signer_type}'"

    if not public_key:
        return "reject", None, [], "signer public key missing"
    if not crypto.verify_event(event_data, public_key):
        return "reject", None, [], "invalid signature"

    signer_info = {
        "type": signer_type,
        "id": signer_id,
        "public_key": public_key,
    }
    return "ok", signer_info, [], None


def resolve_event(
    ref_id: str,
    event_type: str,
    event_data: dict[str, Any],
    recorded_by: str,
    recorded_at: int,
    db: Any,
) -> ResolveResult:
    """Verify signatures and resolve dependencies for a single event."""
    if not isinstance(event_data, dict):
        return ResolveResult(status="reject", ctx=None, error="event_data must be dict")
    if not event_type:
        event_type = event_data.get("type")
    if not event_type:
        return ResolveResult(status="reject", ctx=None, error="event_type missing")
    if event_data.get("type") and event_data.get("type") != event_type:
        return ResolveResult(status="reject", ctx=None, error="event_type mismatch")

    event_spec = registry.get_event_spec(event_type)
    if not event_spec:
        return ResolveResult(status="reject", ctx=None, error="event_spec missing")

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    deps: dict[str, Any] = {}
    missing: list[str] = []

    requires = event_spec.get("requires") or {}
    optional = event_spec.get("optional") or {}

    for dep_name, dep_spec in requires.items():
        status, value, missing_ids, error = _resolve_dep(
            dep_name,
            dep_spec,
            ref_id,
            event_type,
            event_data,
            recorded_by,
            recorded_at,
            safedb,
            unsafedb,
            required=True,
        )
        if status == "reject":
            return ResolveResult(status="reject", ctx=None, error=error)
        if status == "block":
            missing.extend(missing_ids)
            continue
        deps[dep_name] = value

    if missing:
        # Preserve order while de-duplicating.
        seen = set()
        unique_missing = [dep_id for dep_id in missing if not (dep_id in seen or seen.add(dep_id))]
        return ResolveResult(status="block", ctx=None, missing=tuple(unique_missing))

    for dep_name, dep_spec in optional.items():
        status, value, _, error = _resolve_dep(
            dep_name,
            dep_spec,
            ref_id,
            event_type,
            event_data,
            recorded_by,
            recorded_at,
            safedb,
            unsafedb,
            required=False,
        )
        if status == "reject":
            return ResolveResult(status="reject", ctx=None, error=error)
        deps[dep_name] = value

    signer_status, signer, signer_missing, signer_error = _resolve_signer(
        event_spec,
        ref_id,
        event_type,
        event_data,
        recorded_by,
        recorded_at,
        db,
        safedb,
        unsafedb,
    )
    if signer_status == "block":
        return ResolveResult(status="block", ctx=None, missing=tuple(signer_missing))
    if signer_status == "reject":
        return ResolveResult(status="reject", ctx=None, error=signer_error)

    ctx = ProjectionContext(
        event_id=ref_id,
        event_type=event_type,
        event_data=event_data,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        deps=deps,
        signer=signer,
    )
    return ResolveResult(status="ok", ctx=ctx)
