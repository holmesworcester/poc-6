"""Network address event type (peer-announced endpoint observations).

A peer announces observations about another peer's public endpoint.
This supports Byzantine fault tolerance: multiple peers can attest to the same address.

Usage:
  - Alice observes Bob at 203.0.113.5:42000 (from Bob's sync packet)
  - Alice creates address event announcing this observation
  - Charlie receives Alice's observation and learns Bob's endpoint
"""
from typing import Any, Optional
import json
import logging
import crypto
import store
from db import create_safe_db

log = logging.getLogger(__name__)


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


def project(address_id: str, recorded_by: str, recorded_at: int, db: Any) -> Optional[str]:
    """Project network_address event using pure projector.

    Args:
        address_id: Event ID of the address event
        recorded_by: Peer ID that recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection

    Returns:
        address_id if successful, None otherwise
    """
    from projectors import resolve, apply_result
    from projectors import network_address as na_projector

    log.debug(
        f"address.project() address_id={address_id[:20]}..., "
        f"recorded_by={recorded_by[:20]}..."
    )

    # Resolve: parse JSON, gather dependencies (none for this type)
    input_dict = resolve("network_address", address_id, recorded_by, recorded_at, db)
    if not input_dict:
        log.warning(f"address.project() resolve failed for address_id={address_id[:20]}...")
        return None

    # Call pure projector
    result = na_projector.project(input_dict)

    if result.blocked:
        log.info(f"address.project() blocked: {result.missing_deps}")
        return None

    if not result.valid:
        log.warning(f"address.project() rejected: {result.reason}")
        return None

    # Apply result: insert into tables
    apply_result(result, recorded_by, recorded_at, db)

    event_data = input_dict["event_data"]
    log.info(
        f"address.project() inserted: {event_data.get('observed_by_peer_id', '')[:20]}... → "
        f"{event_data.get('observed_peer_id', '')[:20]}... at {event_data.get('ip')}:{event_data.get('port')}"
    )

    return address_id


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
