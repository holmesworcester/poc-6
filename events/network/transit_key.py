"""Transit key event type (device-wide symmetric keys for sync routing).

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    create(peer_id, t_ms, db) -> str
    create_with_material(key_material, peer_id, t_ms, db) -> str
    project_event(key_id, recorded_by, db) -> None
    get_key(key_id, recorded_by, db) -> dict
    get_peer_ids_for_key(key_id, db) -> list[str]
"""
from typing import Any, TypedDict
import json
import logging
import crypto
import store
from db import create_unsafe_db

log = logging.getLogger(__name__)

ID_SIZE = 16  # bytes (128 bits) - BLAKE2b hash size


# ============================================================================
# TYPES
# ============================================================================

class TransitKeyEventData(TypedDict):
    type: str
    key: str  # base64 symmetric key
    signed_by: str  # owner peer_id
    created_at: int


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,
    "signer_type": "none",  # Local-only, deterministic
    "dependencies": [],
    "tables": ["transit_keys"],
    "device_wide": True,
    "generic_dispatch": True,
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result.

    Outputs transit_keys row. Use apply_result_device_wide() to write.
    """
    from projection import ProjectorResult

    event_id = input_dict["event_id"]  # key_id
    event_data = input_dict["event_data"]

    key_b64 = event_data.get("key")
    signed_by = event_data.get("signed_by")  # owner peer_id
    created_at = event_data.get("created_at")

    if not all([key_b64, signed_by, created_at is not None]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Decode key (stored as bytes in DB)
    key = crypto.b64decode(key_b64)

    # Output: transit_keys row (device-wide table)
    row = {
        "key_id": event_id,
        "key": key,
        "owner_peer_id": signed_by,
        "created_at": created_at,
    }

    return ProjectorResult(
        valid=True,
        tables={"transit_keys": [row]},
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 bytes base64
    signed_by: str = "peer_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "transit_key",
        "key": key,
        "signed_by": signed_by,
        "created_at": created_at,
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
    """Create an ephemeral transit key for sync responses (not stored in event log).

    Transit keys are sync protocol infrastructure and don't need to be replayed
    during reprojection. They're created on-demand and stored only in transit_keys table.
    """
    log.info(f"transit_key.create() creating ephemeral transit key for peer_id={peer_id}, t_ms={t_ms}")

    # Generate symmetric key
    key = crypto.generate_secret()

    # Create key ID by hashing the key material (deterministic)
    # Use same approach as store.py but don't store in event log
    event_data = {
        'type': 'transit_key',
        'key': crypto.b64encode(key),
        'signed_by': peer_id,
        'created_at': t_ms
    }
    blob = json.dumps(event_data, separators=(',', ':'), sort_keys=True).encode()
    key_id = crypto.b64encode(crypto.hash(blob))

    # Store directly in transit_keys table (ephemeral, not in event log)
    unsafedb = create_unsafe_db(db)
    unsafedb.execute(
        """INSERT OR IGNORE INTO transit_keys (key_id, key, owner_peer_id, created_at)
           VALUES (?, ?, ?, ?)""",
        (key_id, key, peer_id, t_ms)
    )

    log.warning(f"[TRANSIT_KEY_CREATE] owner={peer_id[:10]}... key_id={key_id} (len={len(key_id)} chars, {len(crypto.b64decode(key_id))} bytes)")
    log.info(f"transit_key.create() created ephemeral key_id={key_id}")
    return key_id


def create_with_material(key_material: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create transit key event with provided key material (for invite transit keys).

    Args:
        key_material: The symmetric key bytes
        peer_id: Peer ID that owns this key
        t_ms: Timestamp
        db: Database connection

    Returns:
        Event ID (to use as hint when wrapping)
    """
    log.info(f"transit_key.create_with_material() creating key for peer_id={peer_id}, t_ms={t_ms}")

    event_data = {
        'type': 'transit_key',
        'key': crypto.b64encode(key_material),
        'signed_by': peer_id,  # Local peer who created this key
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()
    key_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"transit_key.create_with_material() created key_id={key_id}")
    return key_id


# project_event() handled by generic dispatch (SPEC.generic_dispatch = True)


def extract_id(blob: bytes) -> bytes:
    """Extract the first ID_SIZE bytes from a wrapped blob."""
    return blob[:ID_SIZE]


def get_key(key_id: str, recorded_by: str, db: Any) -> dict[str, Any]:
    """Get transit key from database in format expected by crypto.wrap().

    Args:
        key_id: Base64-encoded key ID (event ID)
        recorded_by: Peer ID requesting access (for logging, not enforced for wrapping)
        db: Database connection

    Returns:
        Key dict for crypto.wrap()

    Raises:
        ValueError: If key not found in transit_keys table
    """
    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT key, owner_peer_id FROM transit_keys WHERE key_id = ?", (key_id,))
    if not row:
        raise ValueError(f"transit key not found: {key_id}")

    return {
        'id': crypto.b64decode(key_id),  # Event ID as hint
        'key': row['key'],  # Already bytes from DB
        'type': 'symmetric'
    }


def get_peer_ids_for_key(key_id: str, db: Any) -> list[str]:
    """Get ALL peer_ids that own a specific transit key (for routing).

    Args:
        key_id: Base64-encoded key ID (hint from wrapped blob)
        db: Database connection

    Returns:
        List of peer IDs (may be empty if key not found)
    """
    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one(
        "SELECT owner_peer_id FROM transit_keys WHERE key_id = ?",
        (key_id,)
    )
    if row:
        return [row['owner_peer_id']]

    return []