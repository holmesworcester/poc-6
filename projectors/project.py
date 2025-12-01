"""Projection framework.

Core projection machinery:
  - ProjectorResult: return type from pure projectors
  - resolve(): blob → input_dict (for pure projectors)
  - apply_result(): result → DB writes
  - dispatch(): event_type → projector (replaces big switch in recorded.py)
  - check_deps(): semantic dependency checking (moved from recorded.py)
"""

from dataclasses import dataclass, field
from typing import Any, Callable
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
    # Events to create as side effect of projection (e.g., group_key from group_key_shared)
    derived_events: list[dict] = field(default_factory=list)
    # Events to mark as deleted (framework handles cascade + data cleanup)
    deleted_events: list[str] = field(default_factory=list)


# ============================================================================
# PROJECTOR REGISTRY
# ============================================================================

# Maps event_type -> projector module (for pure projectors)
# or event_type -> (module_path, function_name) for legacy projectors
_PROJECTORS: dict[str, Any] = {}
_LOADED = False


def _load_projectors():
    """Load all projector modules."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True

    # Pure functional projectors (have SPEC + project(input_dict))
    from projectors import message, channel, group_member, user, admin, network, group, peer_shared, invite, invite_accepted
    from projectors import bootstrap_complete, network_joined, transit_prekey_shared, address, group_prekey_shared
    from projectors import group_key_shared, message_deletion, network_address, network_intro, file_slice

    _PROJECTORS["message"] = message
    _PROJECTORS["network_address"] = network_address
    _PROJECTORS["network_intro"] = network_intro
    _PROJECTORS["file_slice"] = file_slice
    _PROJECTORS["message_deletion"] = message_deletion
    _PROJECTORS["channel"] = channel
    _PROJECTORS["group_member"] = group_member
    _PROJECTORS["user"] = user
    _PROJECTORS["admin"] = admin
    _PROJECTORS["network"] = network
    _PROJECTORS["group"] = group
    _PROJECTORS["peer_shared"] = peer_shared
    _PROJECTORS["invite"] = invite
    _PROJECTORS["invite_accepted"] = invite_accepted
    _PROJECTORS["bootstrap_complete"] = bootstrap_complete
    _PROJECTORS["network_joined"] = network_joined
    _PROJECTORS["transit_prekey_shared"] = transit_prekey_shared
    _PROJECTORS["address"] = address
    _PROJECTORS["group_prekey_shared"] = group_prekey_shared
    _PROJECTORS["group_key_shared"] = group_key_shared


def get_spec(event_type: str) -> dict | None:
    """Get SPEC for an event type (if it has a pure projector)."""
    _load_projectors()
    module = _PROJECTORS.get(event_type)
    if module and hasattr(module, 'SPEC'):
        return module.SPEC
    return None


# ============================================================================
# DISPATCH - replaces the big switch in recorded.py
# ============================================================================

def dispatch(event_type: str, ref_id: str, recorded_by: str, recorded_at: int,
             db: Any, event_data: dict | None = None) -> str | None:
    """Dispatch to appropriate projector for event type.

    Returns projected_id on success, None on failure.
    """
    # Pure projectors (use resolve + pure project function)
    _load_projectors()
    if event_type in _PROJECTORS:
        module = _PROJECTORS[event_type]
        if hasattr(module, 'SPEC'):
            # This is a pure projector - but we call the wrapper in events/
            # which handles side effects and uses the pure projector internally
            pass  # Fall through to legacy dispatch for now

    # Legacy dispatch - call the project() function in events/
    # This maintains backward compatibility while we migrate

    if event_type == 'message':
        from events.content import message
        return message.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'message_deletion':
        from events.content import message_deletion
        return message_deletion.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'group':
        from events.group import group
        return group.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'peer':
        from events.identity import peer
        peer.project(ref_id, recorded_by, db)
        return ref_id

    elif event_type == 'transit_key':
        from events.network import transit_key
        transit_key.project(ref_id, recorded_by, db)
        return ref_id

    elif event_type == 'group_key':
        from events.group import group_key
        group_key.project(ref_id, recorded_by, recorded_at, db)
        return ref_id

    elif event_type == 'peer_shared':
        from events.identity import peer_shared
        return peer_shared.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'channel':
        from events.content import channel
        channel.project(ref_id, recorded_by, recorded_at, db)
        return ref_id

    elif event_type == 'sync':
        from events.network import sync
        sync.project(ref_id, recorded_by, recorded_at, db)
        return ref_id

    elif event_type == 'sync_connect':
        from events.network import sync_connect
        sync_connect.project(ref_id, recorded_by, recorded_at, db)
        return ref_id

    elif event_type == 'invite':
        from events.identity import invite
        return invite.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'user':
        from events.identity import user
        return user.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'transit_prekey':
        from events.network import transit_prekey
        transit_prekey.project(ref_id, recorded_by, recorded_at, db)
        return ref_id

    elif event_type == 'transit_prekey_shared':
        from events.network import transit_prekey_shared
        return transit_prekey_shared.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'group_prekey':
        from events.group import group_prekey
        group_prekey.project(ref_id, recorded_by, recorded_at, db)
        return ref_id

    elif event_type == 'group_prekey_shared':
        from events.group import group_prekey_shared
        return group_prekey_shared.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'group_key_shared':
        from events.group import group_key_shared
        return group_key_shared.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'invite_accepted':
        from events.identity import invite_accepted
        return invite_accepted.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'group_member':
        from events.group import group_member
        return group_member.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'network':
        from events.identity import network
        return network.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'admin':
        from events.identity import admin
        return admin.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'network_created':
        from events.identity import network_created
        return network_created.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'network_joined':
        from events.identity import network_joined
        return network_joined.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'bootstrap_complete':
        from events.identity import bootstrap_complete
        return bootstrap_complete.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'address':
        from events.identity import address
        return address.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'file_slice':
        from events.content import file_slice
        file_slice.project(ref_id, event_data, recorded_by, recorded_at, db)
        return ref_id

    elif event_type == 'message_attachment':
        from events.content import message_attachment
        message_attachment.project(ref_id, event_data, recorded_by, recorded_at, db)
        return ref_id

    elif event_type == 'network_address':
        from events.network import address as network_address
        return network_address.project(ref_id, recorded_by, recorded_at, db)

    elif event_type == 'network_intro':
        from events.network import intro as network_intro
        return network_intro.project(ref_id, recorded_by, recorded_at, db)

    # Unknown event type
    log.warning(f"dispatch(): unknown event type: {event_type}")
    return None


# ============================================================================
# CHECK_DEPS - moved from recorded.py
# ============================================================================

def is_foreign_local_dep(field: str, event_data: dict[str, Any], recorded_by: str) -> bool:
    """Check if dependency references another peer's local-only data.

    'Local' is relative to the creator - some deps reference the creator's
    local state (never shared). These should only be checked when we ARE
    the creator, and skipped when we're not.
    """
    event_type = event_data.get('type')
    signed_by = event_data.get('signed_by')

    LOCAL_CREATOR_TYPES = {'transit_key', 'group_key', 'transit_prekey', 'group_prekey'}

    if event_type in LOCAL_CREATOR_TYPES and field == 'signed_by':
        return recorded_by != signed_by

    if event_type == 'peer_shared' and field == 'peer_id':
        creator_peer_id = event_data.get('peer_id')
        return recorded_by != creator_peer_id

    if event_type == 'sync' and field == 'peer_id':
        return True

    if event_type == 'sync_connect' and field == 'peer_id':
        return True

    return False


def check_deps(event_data: dict[str, Any], recorded_by: str, db: Any) -> list[str]:
    """Check dependencies exist in valid_events for this peer.

    Returns list of missing dependency IDs (empty if all satisfied).
    """
    from db import create_safe_db

    safedb = create_safe_db(db, recorded_by=recorded_by)
    event_type = event_data.get('type')

    # Fields to IGNORE as dependencies
    IGNORE_FIELDS = {
        'private_key', 'ref_id', 'file_id',
        'type', 'created_at', 'recorded_at', 'recorded_by',
        'network_id',
    }

    # Event types with NO dependencies
    NO_DEPS_TYPES = {
        'network', 'peer_shared', 'sync_connect', 'peer',
        'group_key', 'transit_key', 'invite_accepted',
    }

    if event_type in NO_DEPS_TYPES:
        return []

    # Determine which fields to check
    SIGNER_ONLY_TYPES = {'message_deletion', 'group_key_shared'}

    if event_type == 'invite':
        dep_fields = ['signed_by', 'admin_grant']
    elif event_type in SIGNER_ONLY_TYPES:
        dep_fields = ['signed_by']
    elif event_type == 'user':
        dep_fields = ['signed_by', 'peer_id', 'invite_id']
    else:
        # DEFAULT: Find all fields ending in '_id' plus known reference fields
        dep_fields = []
        for field, value in event_data.items():
            if field in IGNORE_FIELDS:
                continue
            if field.endswith('_id') and isinstance(value, str) and len(value) > 10:
                dep_fields.append(field)
            elif field in ('signed_by', 'added_by', 'created_by', 'admin_grant'):
                dep_fields.append(field)

    missing_deps = []

    for field in dep_fields:
        dep_id = event_data.get(field)
        if not dep_id:
            continue

        if dep_id in ('SELF', 'PENDING'):
            continue

        if is_foreign_local_dep(field, event_data, recorded_by):
            continue

        valid = safedb.query_one(
            "SELECT 1 FROM valid_events WHERE event_id = ? AND recorded_by = ? LIMIT 1",
            (dep_id, recorded_by)
        )
        if not valid:
            log.warning(f"check_deps() missing dep: {field}={dep_id[:20]}... for peer={recorded_by[:20]}...")
            missing_deps.append(dep_id)

    return missing_deps


# ============================================================================
# GENERIC RESOLVER - for pure projectors
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
    polymorphic_signers = ("invite", "admin", "self", "peer_shared_polymorphic", "invite_polymorphic", "none")
    if signer_type not in polymorphic_signers and not signature_valid:
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
    if signer_type in polymorphic_signers:
        result["signature_valid"] = signature_valid

    return result


def _verify_signature(event_data: dict, signed_by: str, signer_type: str,
                      recorded_by: str, db: Any, safedb: Any, unsafedb: Any) -> bool:
    """Verify event signature based on signer type."""
    import crypto
    import store

    if signer_type == "none":
        return True

    if not signed_by:
        return False

    public_key = None

    if signer_type == "peer_shared":
        from events.identity import peer_shared
        public_key = peer_shared.get_public_key(signed_by, recorded_by, db)

    elif signer_type == "invite":
        invite_id = signed_by
        invite_row = safedb.query_one(
            "SELECT invite_pubkey FROM invites WHERE invite_id = ? AND recorded_by = ? LIMIT 1",
            (invite_id, recorded_by)
        )
        if invite_row:
            public_key = crypto.b64decode(invite_row["invite_pubkey"])
        else:
            invite_blob = store.get(invite_id, unsafedb)
            if invite_blob:
                invite_data = crypto.parse_json(invite_blob)
                pubkey_b64 = invite_data.get("invite_pubkey")
                if pubkey_b64:
                    public_key = crypto.b64decode(pubkey_b64)

    elif signer_type == "network":
        network_row = safedb.query_one(
            "SELECT network_pubkey FROM networks WHERE network_id = ? AND recorded_by = ? LIMIT 1",
            (signed_by, recorded_by)
        )
        if network_row:
            public_key = crypto.b64decode(network_row["network_pubkey"])

    elif signer_type == "self":
        pubkey_field = event_data.get("network_pubkey") or event_data.get("public_key")
        if pubkey_field:
            public_key = crypto.b64decode(pubkey_field)

    elif signer_type == "admin":
        network_id = event_data.get("network_id")
        if signed_by == network_id:
            network_row = safedb.query_one(
                "SELECT network_pubkey FROM networks WHERE network_id = ? AND recorded_by = ? LIMIT 1",
                (signed_by, recorded_by)
            )
            if network_row:
                public_key = crypto.b64decode(network_row["network_pubkey"])
        else:
            from events.identity import peer_shared
            try:
                public_key = peer_shared.get_public_key(signed_by, recorded_by, db)
            except (ValueError, KeyError):
                public_key = None

    elif signer_type == "peer_shared_polymorphic":
        invite_id = event_data.get("invite_id")
        if invite_id and signed_by == invite_id:
            invite_row = safedb.query_one(
                "SELECT invite_pubkey FROM invites WHERE invite_id = ? AND recorded_by = ? LIMIT 1",
                (invite_id, recorded_by)
            )
            if invite_row:
                public_key = crypto.b64decode(invite_row["invite_pubkey"])
            else:
                invite_blob = store.get(invite_id, unsafedb)
                if invite_blob:
                    invite_data = crypto.parse_json(invite_blob)
                    pubkey_b64 = invite_data.get("invite_pubkey")
                    if pubkey_b64:
                        public_key = crypto.b64decode(pubkey_b64)
        else:
            pubkey_b64 = event_data.get("public_key")
            if pubkey_b64:
                public_key = crypto.b64decode(pubkey_b64)

    elif signer_type == "invite_polymorphic":
        network_id = event_data.get("network_id")
        if signed_by == network_id:
            network_row = safedb.query_one(
                "SELECT network_pubkey FROM networks WHERE network_id = ? AND recorded_by = ? LIMIT 1",
                (signed_by, recorded_by)
            )
            if network_row:
                public_key = crypto.b64decode(network_row["network_pubkey"])
            else:
                network_blob = store.get(network_id, unsafedb)
                if network_blob:
                    network_data = crypto.parse_json(network_blob)
                    pubkey_b64 = network_data.get("network_pubkey")
                    if pubkey_b64:
                        public_key = crypto.b64decode(pubkey_b64)
        else:
            from events.identity import peer_shared as ps_module
            try:
                public_key = ps_module.get_public_key(signed_by, recorded_by, db)
            except ValueError:
                user_row = safedb.query_one(
                    "SELECT user_pubkey FROM users WHERE user_id = ? AND recorded_by = ? LIMIT 1",
                    (signed_by, recorded_by)
                )
                if user_row and user_row.get("user_pubkey"):
                    public_key = crypto.b64decode(user_row["user_pubkey"])
                else:
                    user_blob = store.get(signed_by, unsafedb)
                    if user_blob:
                        user_data = crypto.parse_json(user_blob)
                        if user_data.get("type") == "user":
                            pubkey_b64 = user_data.get("user_pubkey")
                            if pubkey_b64:
                                public_key = crypto.b64decode(pubkey_b64)

    if not public_key:
        return False

    return crypto.verify_event(event_data, public_key)


def _resolve_dependency(dep_spec: str, event_id: str, event_data: dict, recorded_by: str,
                        db: Any, safedb: Any, unsafedb: Any) -> tuple[str, Any]:
    """Resolve a single dependency based on spec string.

    Spec format: "name:type" or "name:type?" (optional)
    """
    import crypto
    import store

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
        peer_id = event_data.get(name) or event_data.get("signed_by") or event_data.get("added_by")
        if peer_id:
            row = safedb.query_one(
                "SELECT user_id FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
                (peer_id, recorded_by)
            )
            if row and row["user_id"]:
                result = {"user_id": row["user_id"], "peer_id": peer_id}

    elif dep_type == "admin_grant":
        admin_id = event_data.get("admin_grant")
        if admin_id:
            row = safedb.query_one(
                "SELECT user_id FROM admins WHERE admin_id = ? AND recorded_by = ? LIMIT 1",
                (admin_id, recorded_by)
            )
            if row:
                result = {"event_id": admin_id, "user_id": row["user_id"]}

    elif dep_type == "message_deletion":
        row = safedb.query_one(
            "SELECT deleted_by FROM message_deletions WHERE message_id = ? AND recorded_by = ? LIMIT 1",
            (event_id, recorded_by)
        )
        if row:
            from events.content import message_deletion
            is_valid = message_deletion.validate(event_id, row["deleted_by"], recorded_by, db)
            result = {"deleted_by": row["deleted_by"], "is_valid": is_valid}

    elif dep_type == "invite":
        invite_id = event_data.get("invite_id")
        if invite_id:
            invite_blob = store.get(invite_id, unsafedb)
            if invite_blob:
                result = {
                    "event_id": invite_id,
                    "event_data": crypto.parse_json(invite_blob),
                }

    elif dep_type == "message_event":
        # Look up message for deletion authorization
        # Returns author_id, group_id so we can check if deleter is author/admin
        message_id = event_data.get("message_id")
        if message_id:
            row = safedb.query_one(
                "SELECT author_id, group_id, channel_id FROM messages WHERE message_id = ? AND recorded_by = ?",
                (message_id, recorded_by)
            )
            if row:
                result = {
                    "message_id": message_id,
                    "author_id": row["author_id"],
                    "group_id": row["group_id"],
                    "channel_id": row["channel_id"],
                }

    return name, result


# ============================================================================
# APPLY RESULT - writes to database
# ============================================================================

def apply_result(result: ProjectorResult, recorded_by: str, recorded_at: int, db: Any) -> bool:
    """Apply a projector result to the database.

    All writes use INSERT OR IGNORE - idempotent, append-only.
    Derived events are created after table writes.
    """
    if result.blocked or not result.valid:
        return False

    from db import create_safe_db
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Apply table writes
    for table_name, rows in result.tables.items():
        for row in rows:
            columns = list(row.keys())
            placeholders = ', '.join(['?' for _ in columns])
            column_list = ', '.join(columns)
            values = [row[c] for c in columns]

            sql = f"INSERT OR IGNORE INTO {table_name} ({column_list}) VALUES ({placeholders})"
            safedb.execute(sql, tuple(values))

    # Create derived events
    for derived in result.derived_events:
        event_type = derived.get("type")
        if event_type == "group_key":
            # group_key_shared creates a group_key with pre-computed key material
            from events.group import group_key
            group_key.create_with_material(
                derived["symmetric_key"],  # Raw bytes
                recorded_by,
                derived["created_at"],
                db
            )
            # create_with_material marks it valid
            # notify_event_valid happens automatically in recorded.py
        else:
            log.warning(f"apply_result(): unknown derived event type: {event_type}")

    # Record deleted events (cascade handled by cleanup_deleted_events at end of transaction)
    for event_id in result.deleted_events:
        safedb.execute(
            "INSERT OR IGNORE INTO deleted_events (event_id, recorded_by, deleted_at) VALUES (?, ?, ?)",
            (event_id, recorded_by, recorded_at)
        )
        log.info(f"apply_result(): marked event {event_id[:20]}... as deleted")

    return True


def apply_result_device_wide(result: ProjectorResult, recorded_at: int, db: Any) -> bool:
    """Apply a projector result to DEVICE-WIDE tables.

    Use this for tables that are shared across all peers on this device
    (e.g., sync_connections). These tables don't have recorded_by scoping.

    IMPORTANT: Only use for tables that are intentionally device-wide.
    Most projections should use apply_result() for subjective tables.
    """
    if result.blocked or not result.valid:
        return False

    from db import create_unsafe_db
    unsafedb = create_unsafe_db(db)

    # Apply table writes (no recorded_by scoping)
    for table_name, rows in result.tables.items():
        for row in rows:
            columns = list(row.keys())
            placeholders = ', '.join(['?' for _ in columns])
            column_list = ', '.join(columns)
            values = [row[c] for c in columns]

            # Use INSERT OR REPLACE for device-wide tables (upsert semantics)
            sql = f"INSERT OR REPLACE INTO {table_name} ({column_list}) VALUES ({placeholders})"
            unsafedb.execute(sql, tuple(values))

    return True


# ============================================================================
# DELETION CLEANUP - cascade and data table cleanup
# ============================================================================

# Registry: table name → event_id column name
# Used to clean projected data when events are deleted
DATA_TABLES = {
    'messages': 'message_id',
    'channels': 'channel_id',
    'users': 'user_id',
    'admins': 'admin_id',
    'groups': 'group_id',
    'group_members': 'group_member_id',
    'invites': 'invite_id',
    'peers_shared': 'peer_shared_id',
    # Add more tables as needed
}


def cleanup_deleted_events(recorded_by: str, db: Any) -> int:
    """Enforce invariant: deleted events and orphaned dependents removed.

    Call this at end of transaction, after all projections complete.
    Handles:
    - Directly deleted events (in deleted_events table)
    - Cascade to dependents (parent no longer valid)
    - Subsequent dependencies that arrive later (same mechanism)
    - Projected data in DATA_TABLES

    Returns total count of events removed from valid_events.
    """
    from db import create_safe_db

    safedb = create_safe_db(db, recorded_by=recorded_by)
    total_removed = 0

    # Phase 1: Cascade loop - remove from valid_events
    changed = True
    while changed:
        removed = 0

        # 1. Remove directly deleted from valid_events
        safedb.execute("""
            DELETE FROM valid_events
            WHERE event_id IN (SELECT event_id FROM deleted_events WHERE recorded_by = ?)
            AND recorded_by = ?
        """, (recorded_by, recorded_by))
        removed += safedb.changes()

        # 2. Remove orphaned dependents (parent not in valid_events)
        # This catches: existing reactions, AND future reactions that arrive later
        safedb.execute("""
            DELETE FROM valid_events
            WHERE recorded_by = ?
            AND event_id IN (
                SELECT ed.child_event_id
                FROM event_dependencies ed
                LEFT JOIN valid_events ve
                    ON ed.parent_event_id = ve.event_id
                    AND ve.recorded_by = ed.recorded_by
                WHERE ed.recorded_by = ?
                AND ve.event_id IS NULL
            )
        """, (recorded_by, recorded_by))
        removed += safedb.changes()

        total_removed += removed
        changed = removed > 0

    # Phase 2: Clean projected data from DATA_TABLES
    for table, event_col in DATA_TABLES.items():
        try:
            safedb.execute(f"""
                DELETE FROM {table}
                WHERE recorded_by = ?
                AND {event_col} NOT IN (
                    SELECT event_id FROM valid_events WHERE recorded_by = ?
                )
            """, (recorded_by, recorded_by))
        except Exception as e:
            # Table might not exist or have different schema - log and continue
            log.debug(f"cleanup_deleted_events(): skipping {table}: {e}")

    if total_removed > 0:
        log.info(f"cleanup_deleted_events(): removed {total_removed} events from valid_events for peer {recorded_by[:20]}...")

    return total_removed


# ============================================================================
# PROJECT_EVENT - single entry point for pure projectors
# ============================================================================

def project_event(event_type: str, event_id: str, recorded_by: str, recorded_at: int, db: Any) -> Any:
    """Single entry point for projection using pure projectors."""
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
