"""Network address event type (peer-announced endpoint observations).

A peer announces observations about another peer's public endpoint.
This supports Byzantine fault tolerance: multiple peers can attest to the same address.

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    create(observed_peer_id, observed_by_peer_id, ip, port, t_ms, db) -> str
    project_event(address_id, recorded_by, recorded_at, db) -> str | None
"""
from typing import Any, Optional, TypedDict
import json
import logging
import crypto
import store
from db import create_safe_db

log = logging.getLogger(__name__)


# ============================================================================
# TYPES
# ============================================================================

class NetworkAddressEventData(TypedDict):
    type: str
    observed_peer_id: str
    observed_by_peer_id: str
    ip: str
    port: int
    created_at: int


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,
    "signer_type": "none",  # Local observation, no signature
    "dependencies": [],
    "tables": ["network_addresses"],
    "generic_dispatch": True,
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result.

    Outputs network address observation to network_addresses table.
    """
    from projection import ProjectorResult

    event_id = input_dict["event_id"]
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]
    recorded_at = input_dict["recorded_at"]

    observed_peer_id = event_data.get("observed_peer_id")
    observed_by_peer_id = event_data.get("observed_by_peer_id")
    ip = event_data.get("ip")
    port = event_data.get("port")
    created_at = event_data.get("created_at")

    if not all([observed_peer_id, observed_by_peer_id, ip, port, created_at]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Output: network_addresses row
    row = {
        "address_id": event_id,
        "observed_peer_id": observed_peer_id,
        "observed_by_peer_id": observed_by_peer_id,
        "ip": ip,
        "port": port,
        "created_at": created_at,
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
    }

    return ProjectorResult(
        valid=True,
        tables={"network_addresses": [row]},
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    observed_peer_id: str = "peer_123",
    observed_by_peer_id: str = "peer_456",
    ip: str = "203.0.113.5",
    port: int = 42000,
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "network_address",
        "observed_peer_id": observed_peer_id,
        "observed_by_peer_id": observed_by_peer_id,
        "ip": ip,
        "port": port,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "addr_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_789",
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


def create(
    observed_peer_id: str,
    observed_by_peer_id: str,
    ip: str,
    port: int,
    t_ms: int,
    db: Any
) -> str:
    """Create address event announcing observation of a peer's endpoint.

    Args:
        observed_peer_id: The peer whose endpoint was observed
        observed_by_peer_id: The peer making this observation (creator)
        ip: Observed IP address
        port: Observed port number
        t_ms: Timestamp
        db: Database connection

    Returns:
        address_id: Event ID of the created address event
    """
    log.info(
        f"address.create() {observed_by_peer_id[:20]}... observed "
        f"{observed_peer_id[:20]}... at {ip}:{port}"
    )

    # Create event blob (plaintext JSON, no signing for now)
    event_data = {
        'type': 'network_address',
        'observed_peer_id': observed_peer_id,
        'observed_by_peer_id': observed_by_peer_id,
        'ip': ip,
        'port': port,
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store event with recorded wrapper and projection
    address_id = store.event(blob, observed_by_peer_id, t_ms, db)

    log.info(f"address.create() created address_id={address_id[:20]}...")
    return address_id


# project_event() handled by generic dispatch (SPEC.generic_dispatch = True)


def get_addresses(peer_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """Get all known addresses for a peer from local perspective.

    Args:
        peer_id: The peer whose addresses to look up
        recorded_by: Local peer ID doing the lookup
        db: Database connection

    Returns:
        List of dicts with keys: address_id, ip, port, observed_by_peer_id, created_at
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    rows = safedb.query(
        """SELECT address_id, ip, port, observed_by_peer_id, created_at
           FROM network_addresses
           WHERE observed_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id, recorded_by)
    )

    return [
        {
            'address_id': row['address_id'],
            'ip': row['ip'],
            'port': row['port'],
            'observed_by_peer_id': row['observed_by_peer_id'],
            'created_at': row['created_at']
        }
        for row in rows
    ]


def get_latest_address(peer_id: str, recorded_by: str, db: Any) -> Optional[tuple[str, int]]:
    """Get the most recent known address for a peer.

    Args:
        peer_id: The peer whose address to look up
        recorded_by: Local peer ID doing the lookup
        db: Database connection

    Returns:
        (ip, port) tuple or None if no addresses known
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    row = safedb.query_one(
        """SELECT ip, port FROM network_addresses
           WHERE observed_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC LIMIT 1""",
        (peer_id, recorded_by)
    )

    if row:
        return (row['ip'], row['port'])
    return None
