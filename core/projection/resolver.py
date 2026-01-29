"""Resolver for projection v2."""
from __future__ import annotations

from typing import Any
import os

from core import crypto, store
from core.db import SUBJECTIVE_TABLES, create_safe_db, create_unsafe_db
from events import registry
from events.identity import invite
from .types import ProjectionContext, ResolveResult

_CONTEXT_KEYS = {
    "@recorded_by",
    "@recorded_at",
    "@event_id",
    "@event_type",
}


def _chunked(items: list[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        return [items]
    return [items[i:i + size] for i in range(0, len(items), size)]


def _is_event_valid(event_id: str, recorded_by: str, safedb: Any,
                    dep_cache: dict[str, Any] | None = None) -> bool:
    if dep_cache is not None:
        valid = dep_cache.get("valid_events", {}).get(recorded_by)
        if valid is not None and event_id in valid:
            return True

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
    dep_cache: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if fields:
        columns = ", ".join(fields)
    else:
        columns = "*"
    where_clauses = [f"{key_field} = ?"]
    params: list[Any] = [key_value]
    use_safe = table in SUBJECTIVE_TABLES
    if dep_cache is not None:
        table_cache = dep_cache.get("tables", {}).get(table, {})
        key_cache = table_cache.get(key_field, {})
        if use_safe:
            peer_cache = key_cache.get(recorded_by, {})
            cached = peer_cache.get(key_value)
        else:
            cached = key_cache.get(None, {}).get(key_value)
        if cached is not None:
            return cached
    if use_safe and key_field not in ("recorded_by", "can_share_peer_id"):
        where_clauses.append("recorded_by = ?")
        params.append(recorded_by)
    sql = f"SELECT {columns} FROM {table} WHERE {' AND '.join(where_clauses)}"
    target_db = safedb if use_safe else unsafedb
    row = target_db.query_one(sql, tuple(params))
    if row is not None and dep_cache is not None:
        table_cache = dep_cache.setdefault("tables", {}).setdefault(table, {})
        key_cache = table_cache.setdefault(key_field, {})
        peer_key = recorded_by if use_safe else None
        peer_cache = key_cache.setdefault(peer_key, {})
        peer_cache[key_value] = row
    return row


def _resolve_table_dep(
    dep_name: str,
    dep_spec: dict[str, Any],
    event_data: dict[str, Any],
    recorded_by: str,
    safedb: Any,
    unsafedb: Any,
    required: bool,
    dep_cache: dict[str, Any] | None = None,
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
        if required or dep_spec.get("required_if_present"):
            return "reject", None, [], f"dep '{dep_name}' invalid id type"
        return "ok", None, [], None
    if not _is_event_valid(dep_id, recorded_by, safedb, dep_cache=dep_cache):
        if required or dep_spec.get("required_if_present"):
            return "block", None, [dep_id], None
        return "ok", None, [], None
    table = dep_spec.get("table")
    if not table:
        return "reject", None, [], f"dep '{dep_name}' missing table"
    fields = dep_spec.get("fields")
    row = _fetch_dep_row(table, fields, key_field, dep_id, recorded_by, safedb, unsafedb, dep_cache=dep_cache)
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
    dep_cache: dict[str, Any] | None = None,
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
    row = _fetch_dep_row(
        table,
        dep_spec.get("fields"),
        key_field,
        lookup_value,
        recorded_by,
        safedb,
        unsafedb,
        dep_cache=dep_cache,
    )
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
    dep_cache: dict[str, Any] | None = None,
) -> tuple[str, Any | None, list[str], str | None]:
    source = dep_spec.get("source")
    if source == "table":
        return _resolve_table_dep(
            dep_name, dep_spec, event_data, recorded_by, safedb, unsafedb, required, dep_cache=dep_cache
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
            dep_cache=dep_cache,
        )
    return "reject", None, [], f"dep '{dep_name}' has unknown source"



def prefetch_dependencies(
    contexts: list[dict[str, Any]],
    db: Any,
) -> dict[str, Any]:
    """Prefetch dependency rows for a batch of events.

    Returns a cache dict used by resolve_event to avoid repeated queries.
    """
    dep_cache: dict[str, Any] = {"tables": {}, "valid_events": {}}
    if not contexts:
        return dep_cache

    unsafedb = create_unsafe_db(db)

    dep_requests: dict[tuple[str, str, str | None], set[Any]] = {}
    valid_requests: dict[str, set[str]] = {}

    for ctx in contexts:
        event_data = ctx.get("event_data")
        event_type = ctx.get("event_type")
        recorded_by = ctx.get("recorded_by")
        if not isinstance(event_data, dict) or not event_type or not recorded_by:
            continue
        event_spec = registry.get_event_spec(event_type) or {}
        for dep_spec in (event_spec.get("requires") or {}).values():
            _collect_dep_request(dep_spec, event_data, recorded_by, dep_requests, valid_requests)
        for dep_spec in (event_spec.get("optional") or {}).values():
            _collect_dep_request(dep_spec, event_data, recorded_by, dep_requests, valid_requests)

        signer_spec = event_spec.get("signer") or {}
        signer_type_field = signer_spec.get("type_field")
        signer_id_field = signer_spec.get("id_field")
        if signer_type_field and signer_id_field:
            signer_type = event_data.get(signer_type_field)
            signer_id = event_data.get(signer_id_field)
            if isinstance(signer_id, str):
                if signer_type in ("peer_shared", "network", "user"):
                    valid_requests.setdefault(recorded_by, set()).add(signer_id)

                signer_table: tuple[str, str] | None = None
                if signer_type == "peer_shared":
                    signer_table = ("peers_shared", "peer_shared_id")
                elif signer_type == "network":
                    if not _field_looks_like_pubkey(signer_id_field):
                        signer_table = ("networks", "network_id")
                elif signer_type == "user":
                    signer_table = ("users", "user_id")
                elif signer_type == "invite":
                    signer_table = ("invites", "invite_id")

                if signer_table:
                    table, key_field = signer_table
                    dep_requests.setdefault((table, key_field, recorded_by), set()).add(signer_id)
                    if signer_type == "invite":
                        dep_requests.setdefault(
                            ("invite_accepteds", "invite_id", recorded_by),
                            set()
                        ).add(signer_id)

    chunk_size = int(os.getenv("PROJECT_DEP_CHUNK", "400"))

    for recorded_by, event_ids in valid_requests.items():
        if not event_ids:
            continue
        valid_set: set[str] = set()
        safedb = create_safe_db(db, recorded_by)
        for chunk in _chunked(list(event_ids), chunk_size):
            placeholders = ",".join("?" for _ in chunk)
            rows = safedb.query(
                f"SELECT event_id FROM valid_events WHERE recorded_by = ? AND event_id IN ({placeholders})",
                tuple([recorded_by, *chunk]),
            )
            valid_set.update(row["event_id"] for row in rows)
        dep_cache["valid_events"][recorded_by] = valid_set

    for (table, key_field, recorded_by), values in dep_requests.items():
        if not values:
            continue
        is_subjective = table in SUBJECTIVE_TABLES
        columns = "*"
        table_cache = dep_cache["tables"].setdefault(table, {})
        key_cache = table_cache.setdefault(key_field, {})
        peer_key = recorded_by if is_subjective else None
        peer_cache = key_cache.setdefault(peer_key, {})

        for chunk in _chunked(list(values), chunk_size):
            placeholders = ",".join("?" for _ in chunk)
            params: list[Any] = list(chunk)
            sql = f"SELECT {columns} FROM {table} WHERE {key_field} IN ({placeholders})"
            if is_subjective and key_field not in ("recorded_by", "can_share_peer_id"):
                sql += " AND recorded_by = ?"
                params.append(recorded_by)
            rows = unsafedb.query(sql, tuple(params)) if not is_subjective else create_safe_db(db, recorded_by).query(sql, tuple(params))
            for row in rows:
                row_key = row.get(key_field)
                if row_key is not None:
                    peer_cache[row_key] = row

    return dep_cache


def _collect_dep_request(
    dep_spec: dict[str, Any],
    event_data: dict[str, Any],
    recorded_by: str,
    dep_requests: dict[tuple[str, str, str | None], set[Any]],
    valid_requests: dict[str, set[str]],
) -> None:
    if dep_spec.get("source") != "table":
        return
    key_field = dep_spec.get("key")
    if not key_field:
        return
    value_field = dep_spec.get("key_from") or key_field
    dep_id = event_data.get(value_field)
    if not isinstance(dep_id, str):
        return
    table = dep_spec.get("table")
    if not table:
        return
    dep_requests.setdefault((table, key_field, recorded_by), set()).add(dep_id)
    valid_requests.setdefault(recorded_by, set()).add(dep_id)


def _resolve_invite_pubkey(
    invite_id: str,
    recorded_by: str,
    safedb: Any,
    unsafedb: Any,
    dep_cache: dict[str, Any] | None = None,
) -> bytes | None:
    row = _fetch_dep_row(
        "invites",
        ["invite_pubkey"],
        "invite_id",
        invite_id,
        recorded_by,
        safedb,
        unsafedb,
        dep_cache=dep_cache,
    )
    if row and row.get("invite_pubkey"):
        return crypto.b64decode(row["invite_pubkey"])

    row = _fetch_dep_row(
        "invite_accepteds",
        ["invite_pubkey"],
        "invite_id",
        invite_id,
        recorded_by,
        safedb,
        unsafedb,
        dep_cache=dep_cache,
    )
    if row and row.get("invite_pubkey"):
        return crypto.b64decode(row["invite_pubkey"])

    # Fallback: check blob store
    blob = store.get(invite_id, unsafedb)
    if not blob:
        return None
    if blob[:1] in (b'{', b'['):
        raise ValueError("JSON invite blobs are no longer supported")
    if not invite.is_wire_envelope(blob):
        return None
    invite_data = invite.decode_wire_event(blob)
    invite_pubkey = invite_data.get("invite_pubkey")
    if not invite_pubkey:
        return None
    return crypto.b64decode(invite_pubkey)


def _signature_payload(
    event_data: dict[str, Any],
) -> tuple[bytes, bytes] | None:
    wire_signature = event_data.get("_wire_signature") if isinstance(event_data, dict) else None
    wire_signed_bytes = event_data.get("_wire_signed_bytes") if isinstance(event_data, dict) else None
    if wire_signature is not None or wire_signed_bytes is not None:
        if not isinstance(wire_signature, (bytes, bytearray)) or not isinstance(wire_signed_bytes, (bytes, bytearray)):
            return None
        return (bytes(wire_signed_bytes), bytes(wire_signature))

    return None


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
    dep_cache: dict[str, Any] | None = None,
    verify_queue: list[tuple[str, bytes, bytes, bytes]] | None = None,
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
        if not _is_event_valid(signer_id, recorded_by, safedb, dep_cache=dep_cache):
            return "block", None, [signer_id], None
        row = _fetch_dep_row(
            "peers_shared",
            ["public_key", "user_id"],
            "peer_shared_id",
            signer_id,
            recorded_by,
            safedb,
            unsafedb,
            dep_cache=dep_cache,
        )
        if not row or not row.get("public_key"):
            return "reject", None, [], "peer_shared signer not available"
        public_key = crypto.b64decode(row["public_key"])
        signer_user_id = row.get("user_id")
        signer_is_admin = None
        if signer_user_id:
            network_row = safedb.query_one(
                "SELECT network_id FROM networks WHERE recorded_by = ? LIMIT 1",
                (recorded_by,),
            )
            if network_row and network_row.get("network_id"):
                admin_row = safedb.query_one(
                    "SELECT 1 FROM admins WHERE user_id = ? AND network_id = ? AND recorded_by = ? LIMIT 1",
                    (signer_user_id, network_row["network_id"], recorded_by),
                )
                signer_is_admin = admin_row is not None
    elif signer_type == "invite":
        # For invite signer, don't require the invite event in valid_events.
        # Joiners have invite_accepted (not invite), so we just try to resolve the pubkey
        # from invites table, invite_accepteds table, or blob store.
        public_key = _resolve_invite_pubkey(
            signer_id,
            recorded_by,
            safedb,
            unsafedb,
            dep_cache=dep_cache,
        )
        if not public_key:
            return "reject", None, [], "invite signer not available"
    elif signer_type == "network":
        if _field_looks_like_pubkey(id_field):
            try:
                public_key = crypto.b64decode(signer_id)
            except Exception:
                return "reject", None, [], "invalid network pubkey"
        else:
            if not _is_event_valid(signer_id, recorded_by, safedb, dep_cache=dep_cache):
                return "block", None, [signer_id], None
            row = _fetch_dep_row(
                "networks",
                ["network_pubkey"],
                "network_id",
                signer_id,
                recorded_by,
                safedb,
                unsafedb,
                dep_cache=dep_cache,
            )
            if not row or not row.get("network_pubkey"):
                # Network is valid (trust anchor) but not projected yet - block until it projects
                return "block", None, [signer_id], None
            public_key = crypto.b64decode(row["network_pubkey"])
    elif signer_type == "user":
        # User signer - used by first peer invite (signed by user_id during bootstrap)
        if not _is_event_valid(signer_id, recorded_by, safedb, dep_cache=dep_cache):
            return "block", None, [signer_id], None
        # Get user's public key from users table
        user_row = _fetch_dep_row(
            "users",
            ["user_pubkey"],
            "user_id",
            signer_id,
            recorded_by,
            safedb,
            unsafedb,
            dep_cache=dep_cache,
        )
        if not user_row or not user_row.get("user_pubkey"):
            return "reject", None, [], "user signer not available"
        try:
            public_key = crypto.b64decode(user_row["user_pubkey"])
        except Exception:
            return "reject", None, [], "invalid user pubkey"
    else:
        return "reject", None, [], f"unknown signer type '{signer_type}'"

    if not public_key:
        return "reject", None, [], "signer public key missing"

    signer_info = {
        "type": signer_type,
        "id": signer_id,
        "public_key": public_key,
    }
    if signer_type == "peer_shared":
        signer_info["user_id"] = signer_user_id
        signer_info["is_admin"] = signer_is_admin

    payload = _signature_payload(event_data)
    if not payload:
        return "reject", None, [], "missing signature"
    signed_bytes, signature = payload

    if verify_queue is not None:
        verify_queue.append((ref_id, signed_bytes, signature, public_key))
        return "ok", signer_info, [], None

    if not crypto.verify(signed_bytes, signature, public_key):
        return "reject", None, [], "invalid signature"
    return "ok", signer_info, [], None


def _check_trust_anchor(
    event_spec: dict[str, Any],
    ref_id: str,
    recorded_by: str,
    safedb: Any,
) -> tuple[str, str | None]:
    """Check trust anchor requirement if specified in event_spec.

    Returns:
        (status, error) - status is 'ok' or 'reject', error is message if rejected
    """
    trust_anchor_spec = event_spec.get("trust_anchor")
    if not trust_anchor_spec:
        return "ok", None

    table = trust_anchor_spec.get("table")
    key = trust_anchor_spec.get("key")
    key_from = trust_anchor_spec.get("key_from")

    if not table or not key:
        return "reject", "trust_anchor spec missing table or key"

    # Resolve key value
    if key_from == "@event_id":
        key_value = ref_id
    else:
        return "reject", f"trust_anchor key_from '{key_from}' not supported"

    # Check if trust anchor exists
    trust_anchor = safedb.query_one(
        f"SELECT 1 FROM {table} WHERE {key} = ? AND recorded_by = ?",
        (key_value, recorded_by)
    )
    if trust_anchor:
        return "ok", None

    # Also accept if already projected (for idempotency)
    # This handles re-projection and existing valid events
    already_valid = safedb.query_one(
        "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ?",
        (ref_id, recorded_by)
    )
    if already_valid:
        return "ok", None

    return "reject", f"trust anchor not found in {table}"


def resolve_event(
    ref_id: str,
    event_type: str,
    event_data: dict[str, Any],
    recorded_by: str,
    recorded_at: int,
    db: Any,
    dep_cache: dict[str, Any] | None = None,
    verify_queue: list[tuple[str, bytes, bytes, bytes]] | None = None,
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

    # Check trust anchor requirement (e.g., network events need trust anchor before projection)
    trust_status, trust_error = _check_trust_anchor(event_spec, ref_id, recorded_by, safedb)
    if trust_status == "reject":
        return ResolveResult(status="reject", ctx=None, error=trust_error)

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
            dep_cache=dep_cache,
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
            required=False,
            dep_cache=dep_cache,
        )
        if status == "reject":
            return ResolveResult(status="reject", ctx=None, error=error)
        if status == "block":
            # Optional dep with required_if_present=True is blocking
            missing.extend(missing_ids)
            continue
        deps[dep_name] = value

    # Check if optional deps caused blocking (required_if_present)
    if missing:
        seen = set()
        unique_missing = [dep_id for dep_id in missing if not (dep_id in seen or seen.add(dep_id))]
        return ResolveResult(status="block", ctx=None, missing=tuple(unique_missing))

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
        dep_cache=dep_cache,
        verify_queue=verify_queue,
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
