"""Pure Functional Projectors.

Each projector defines:
  SPEC - declares encrypted, signer_type, dependencies, tables
  project(input_dict) - pure function: dict -> ProjectorResult

The framework handles:
  resolve() - generic resolution driven by SPEC
  apply_result() - writes to database with INSERT OR IGNORE
"""

from dataclasses import dataclass, field
from typing import Any
import logging

log = logging.getLogger(__name__)


@dataclass
class ProjectorResult:
    """Result from a pure projector function."""
    valid: bool = True
    reason: str | None = None
    tables: dict[str, list[dict]] = field(default_factory=dict)
    blocked: bool = False
    missing_deps: list[str] = field(default_factory=list)


# ============================================================================
# GENERIC RESOLVER - driven by SPEC
# ============================================================================

def resolve(event_type: str, event_id: str, recorded_by: str, recorded_at: int, db: Any) -> dict | None:
    """Generic resolver - builds input dict based on projector SPEC.

    Steps:
    1. Get blob from store
    2. Unwrap (encrypted) or parse (plaintext) based on SPEC
    3. Verify signature based on signer_type
    4. Resolve declared dependencies
    5. Return input dict for pure projector
    """
    import crypto
    import store
    from db import create_safe_db, create_unsafe_db

    _load_projectors()
    module = _PROJECTORS.get(event_type)
    if not module:
        log.warning(f"No projector for {event_type}")
        return None

    spec = module.SPEC
    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # 1. Get blob
    blob = store.get(event_id, unsafedb)
    if not blob:
        return None

    # 2. Unwrap or parse
    if spec.get("encrypted", True):
        unwrapped, _ = crypto.unwrap(blob, recorded_by, db)
        if not unwrapped:
            return None
        event_data = crypto.parse_json(unwrapped)
        key_id = crypto.b64encode(blob[:crypto.ID_SIZE])
    else:
        event_data = crypto.parse_json(blob)
        key_id = None

    # 3. Verify signature
    signer_type = spec.get("signer_type", "peer_shared")
    signer_field = spec.get("signer_field", "signed_by")
    signed_by = event_data.get(signer_field)

    signature_valid = _verify_signature(
        event_data, signed_by, signer_type, recorded_by, db, safedb, unsafedb
    )

    # For most events, invalid signature = reject
    # For user events, we pass signature_valid to the projector
    if signer_type != "invite" and not signature_valid:
        return None

    # 4. Resolve dependencies
    deps = {}
    for dep_spec in spec.get("dependencies", []):
        dep_name, dep_value = _resolve_dependency(
            dep_spec, event_id, event_data, recorded_by, db, safedb, unsafedb
        )
        deps[dep_name] = dep_value

    # 5. Build input dict
    result = {
        "event_id": event_id,
        "event_data": event_data,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": deps,
    }

    if key_id:
        result["key_id"] = key_id
    if signer_type == "invite":
        result["signature_valid"] = signature_valid

    return result


def _verify_signature(event_data: dict, signed_by: str, signer_type: str,
                      recorded_by: str, db: Any, safedb: Any, unsafedb: Any) -> bool:
    """Verify event signature based on signer type."""
    import crypto
    import store

    if not signed_by:
        return False

    public_key = None

    if signer_type == "peer_shared":
        from events.identity import peer_shared
        public_key = peer_shared.get_public_key(signed_by, recorded_by, db)

    elif signer_type == "invite":
        # For invite-signed events (like user), get key from invites table or blob
        invite_id = signed_by
        invite_row = safedb.query_one(
            "SELECT invite_pubkey FROM invites WHERE invite_id = ? AND recorded_by = ? LIMIT 1",
            (invite_id, recorded_by)
        )
        if invite_row:
            public_key = crypto.b64decode(invite_row["invite_pubkey"])
        else:
            # Bootstrap: try blob
            invite_blob = store.get(invite_id, unsafedb)
            if invite_blob:
                invite_data = crypto.parse_json(invite_blob)
                pubkey_b64 = invite_data.get("invite_pubkey")
                if pubkey_b64:
                    public_key = crypto.b64decode(pubkey_b64)

    elif signer_type == "network":
        # Network-signed events
        network_row = safedb.query_one(
            "SELECT network_pubkey FROM networks WHERE network_id = ? AND recorded_by = ? LIMIT 1",
            (signed_by, recorded_by)
        )
        if network_row:
            public_key = crypto.b64decode(network_row["network_pubkey"])

    if not public_key:
        return False

    return crypto.verify_event(event_data, public_key)


def _resolve_dependency(dep_spec: str, event_id: str, event_data: dict, recorded_by: str,
                        db: Any, safedb: Any, unsafedb: Any) -> tuple[str, Any]:
    """Resolve a single dependency based on spec string.

    Spec format: "name:type" or "name:type?" (optional)
    Examples: "deletion:message_deletion?", "group:event", "signer_user:linked_peer"
    """
    import crypto
    import store

    # Parse spec
    optional = dep_spec.endswith("?")
    if optional:
        dep_spec = dep_spec[:-1]

    if ":" in dep_spec:
        name, dep_type = dep_spec.split(":", 1)
    else:
        name = dep_spec
        dep_type = "event"

    result = None

    if dep_type == "event":
        # Generic event lookup - get dep_event_id from event_data[name] or event_data[name + "_id"]
        dep_event_id = event_data.get(name) or event_data.get(f"{name}_id")
        if dep_event_id:
            blob = store.get(dep_event_id, unsafedb)
            if blob:
                unwrapped, _ = crypto.unwrap(blob, recorded_by, db)
                if unwrapped:
                    result = {
                        "event_id": dep_event_id,
                        "event_data": crypto.parse_json(unwrapped),
                    }

    elif dep_type == "linked_peer":
        # Lookup user_id from peers_shared for a peer_shared_id
        # (Schema change: user_id moved from linked_peers to peers_shared)
        peer_id = event_data.get(name) or event_data.get("signed_by") or event_data.get("added_by")
        if peer_id:
            row = safedb.query_one(
                "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
                (peer_id, recorded_by)
            )
            if row and row["user_id"]:
                result = {"user_id": row["user_id"], "peer_id": peer_id}

    elif dep_type == "admin_grant":
        # Lookup admin grant from event_data.admin_grant
        admin_id = event_data.get("admin_grant")
        if admin_id:
            row = safedb.query_one(
                "SELECT user_id FROM admins WHERE admin_id = ? AND recorded_by = ? LIMIT 1",
                (admin_id, recorded_by)
            )
            if row:
                result = {"event_id": admin_id, "user_id": row["user_id"]}

    elif dep_type == "message_deletion":
        # Check for deletion - uses the event_id (message_id)
        row = safedb.query_one(
            "SELECT deleted_by FROM message_deletions WHERE message_id = ? AND recorded_by = ? LIMIT 1",
            (event_id, recorded_by)
        )
        if row:
            from events.content import message_deletion
            is_valid = message_deletion.validate(event_id, row["deleted_by"], recorded_by, db)
            result = {"deleted_by": row["deleted_by"], "is_valid": is_valid}

    elif dep_type == "invite":
        # Get invite from table or blob
        invite_id = event_data.get("invite_id")
        if invite_id:
            invite_blob = store.get(invite_id, unsafedb)
            if invite_blob:
                result = {
                    "event_id": invite_id,
                    "event_data": crypto.parse_json(invite_blob),
                }

    return name, result


# ============================================================================
# APPLY RESULT - writes to database
# ============================================================================

def apply_result(result: ProjectorResult, recorded_by: str, recorded_at: int, db: Any) -> bool:
    """Apply a projector result to the database.

    All writes use INSERT OR IGNORE - idempotent, append-only.
    """
    if result.blocked or not result.valid:
        return False

    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=recorded_by)

    for table_name, rows in result.tables.items():
        for row in rows:
            columns = list(row.keys())
            placeholders = ', '.join(['?' for _ in columns])
            column_list = ', '.join(columns)
            values = [row[c] for c in columns]

            sql = f"INSERT OR IGNORE INTO {table_name} ({column_list}) VALUES ({placeholders})"
            safedb.execute(sql, tuple(values))

    return True


# ============================================================================
# REGISTRY
# ============================================================================

_PROJECTORS: dict[str, Any] = {}


def _load_projectors():
    """Load all projector modules."""
    if _PROJECTORS:
        return

    from projectors import message, channel, group_member, user

    _PROJECTORS["message"] = message
    _PROJECTORS["channel"] = channel
    _PROJECTORS["group_member"] = group_member
    _PROJECTORS["user"] = user


def project_event(event_type: str, event_id: str, recorded_by: str, recorded_at: int, db: Any) -> Any:
    """Single entry point for projection."""
    _load_projectors()

    module = _PROJECTORS.get(event_type)
    if not module:
        return None

    input_dict = resolve(event_type, event_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = module.project(input_dict)

    if result.blocked or not result.valid:
        return result

    apply_result(result, recorded_by, recorded_at, db)
    return result
