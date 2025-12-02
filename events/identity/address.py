"""Address event type (peer's network address for direct communication).

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    create(peer_id, peer_shared_id, ip, port, t_ms, db) -> str
    project_event(address_id, recorded_by, recorded_at, db) -> str | None
"""
from typing import Any, TypedDict
import logging
import crypto
import store
from events.identity import peer
from db import create_safe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class AddressEventData(TypedDict):
    type: str
    peer_id: str  # peer_shared_id
    signed_by: str
    ip: str
    port: int
    created_at: int


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result."""
    from projection import ProjectorResult

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    peer_shared_id = event_data.get("peer_id")
    ip = event_data.get("ip")
    port = event_data.get("port")
    created_at = event_data.get("created_at")

    if not all([peer_shared_id, ip, port is not None, created_at]):
        return ProjectorResult(valid=False, reason="Missing required fields")

    row = {
        "address_id": event_id,
        "peer_shared_id": peer_shared_id,
        "ip": ip,
        "port": port,
        "created_at": created_at,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(valid=True, tables={"addresses": [row]})


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    peer_id: str = "ps_123",
    signed_by: str = "ps_123",
    ip: str = "127.0.0.1",
    port: int = 6100,
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "address",
        "peer_id": peer_id,
        "signed_by": signed_by,
        "ip": ip,
        "port": port,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "addr_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_456",
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


def create(peer_id: str, peer_shared_id: str, ip: str, port: int, t_ms: int, db: Any) -> str:
    """Create an address event for a peer.

    Args:
        peer_id: Local peer ID (creator)
        peer_shared_id: Public peer shared ID
        ip: IP address
        port: Port number
        t_ms: Timestamp
        db: Database connection

    Returns:
        address_id: Event ID of the created address event
    """
    log.info(f"address.create() creating address for peer_shared_id={peer_shared_id[:20]}..., ip={ip}, port={port}")

    # Get peer's private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Create event dict (plaintext, will be signed)
    event_data = {
        'type': 'address',
        'peer_id': peer_shared_id,
        'signed_by': peer_shared_id,
        'ip': ip,
        'port': port,
        'created_at': t_ms
    }

    # Sign the event with peer's private key
    signed_event = crypto.sign_event(event_data, private_key)

    # Canonicalize to get deterministic blob
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    address_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"address.create() created address_id={address_id[:20]}...")
    return address_id


def project_event(address_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project address event into addresses table."""
    from projection import resolve

    input_dict = resolve("address", address_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = project(input_dict)

    if not result.valid:
        log.warning(f"address.project_event() failed: {result.reason}")
        return None

    # Use INSERT OR REPLACE for addresses (may update existing)
    safedb = create_safe_db(db, recorded_by=recorded_by)
    for row in result.tables.get("addresses", []):
        safedb.execute(
            """INSERT OR REPLACE INTO addresses
               (address_id, peer_shared_id, ip, port, created_at, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (row["address_id"], row["peer_shared_id"], row["ip"], row["port"],
             row["created_at"], row["recorded_by"], row["recorded_at"])
        )

    return address_id
