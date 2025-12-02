"""Group key event type (subjective symmetric keys for network/group content encryption).

Note: group_key events are DETERMINISTIC (no timestamp in blob).
created_at comes from recorded_at parameter.

SPEC/DEPS - declarative metadata for generic resolver
project() - pure function: input_dict -> ProjectorResult
create_pure() - pure function: deps -> CreateResult

API functions:
    create(peer_id, t_ms, db) -> str
    create_with_material(key_material, peer_id, t_ms, db) -> str
    project_event(key_id, recorded_by, recorded_at, db) -> None
    get_key(key_id, recorded_by, db) -> dict
"""
from typing import Any, TypedDict
import json
import logging
import crypto
import store
from db import create_safe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class GroupKeyEventData(TypedDict):
    type: str
    key: str  # base64 symmetric key


class GroupKeyCreateDeps(TypedDict):
    """Dependencies for group_key creation."""
    key_material: bytes  # Generated symmetric key (32 bytes)


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,
    "signer_type": "none",  # Local-only, deterministic
    "dependencies": [],
    "tables": ["group_keys"],
    "mark_valid": True,  # Mark in valid_events
    "generic_dispatch": True,
}


# ============================================================================
# DEPS - dependencies needed for creation
# ============================================================================

DEPS = {
    "key_material": {"type": "generated_secret"},
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result. No database access."""
    from projection import ProjectorResult

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    key_b64 = event_data.get("key")

    if not key_b64:
        return ProjectorResult(valid=False, reason="missing required field: key")

    key = crypto.b64decode(key_b64)

    row = {
        "key_id": event_id,
        "key": key,
        "created_at": recorded_at,  # Deterministic blobs have no timestamp
        "recorded_by": recorded_by,
    }

    return ProjectorResult(valid=True, tables={"group_keys": [row]})


def create_pure(deps: GroupKeyCreateDeps):
    """Pure function to create a group_key event.

    DETERMINISTIC - same key material produces same key_id.
    No timestamp in blob ensures identical content-addressed IDs across peers.
    """
    from projection import CreateResult, BlobSpec, compute_event_id

    event_data = {
        'type': 'group_key',
        'key': crypto.b64encode(deps['key_material']),
    }

    blob = json.dumps(event_data, sort_keys=True).encode()
    key_id = compute_event_id(blob)

    return CreateResult(
        blobs=[BlobSpec(blob=blob, event_id=key_id, event_type='group_key')],
        primary_id=key_id,
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
) -> dict:
    """Build event_data for testing (deterministic - no timestamp)."""
    return {
        "type": "group_key",
        "key": key,
    }


def make_input(
    event_id: str = "key_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_123",
    recorded_at: int = 1000001,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }


# ============================================================================
# API FUNCTIONS
# ============================================================================

def create(peer_id: str, t_ms: int, db: Any) -> str:
    """Create a group key for network content encryption."""
    from projection import store_create_result

    deps = {'key_material': crypto.generate_secret()}
    result = create_pure(deps)
    key_id = store_create_result(result, peer_id, t_ms, db)

    log.info(f"group_key.create() created key_id={key_id[:20]}...")
    return key_id


def create_with_material(key_material: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create group key event with provided key material (deterministic)."""
    from projection import store_create_result

    deps = {'key_material': key_material}
    result = create_pure(deps)
    key_id = store_create_result(result, peer_id, t_ms, db)

    log.info(f"group_key.create_with_material() created key_id={key_id[:20]}...")
    return key_id


# project_event() handled by generic dispatch (SPEC.generic_dispatch = True)


def get_key(key_id: str, recorded_by: str, db: Any) -> dict[str, Any]:
    """Get group key from database in format expected by crypto.wrap()."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT key FROM group_keys WHERE key_id = ? AND recorded_by = ?",
        (key_id, recorded_by)
    )
    if not row:
        raise ValueError(f"group key not found: {key_id}")

    return {
        'id': crypto.b64decode(key_id),
        'key': row['key'],
        'type': 'symmetric'
    }


def get_or_create_clean_key(group_id: str, peer_id: str, t_ms: int, db: Any) -> str:
    """Get an existing clean key or create a new one if needed.

    A "clean" key is one NOT in the keys_to_purge table.
    Used during forward secrecy rekeying.
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    clean_key_row = safedb.query_one(
        """SELECT gk.key_id FROM group_keys gk
           LEFT JOIN keys_to_purge ktp ON gk.key_id = ktp.key_id AND ktp.recorded_by = ?
           WHERE gk.recorded_by = ? AND ktp.key_id IS NULL
           ORDER BY gk.created_at DESC
           LIMIT 1""",
        (peer_id, peer_id)
    )

    if clean_key_row:
        return clean_key_row['key_id']

    return create(peer_id, t_ms, db)
