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
    """Project address event into network_addresses table.

    Args:
        address_id: Event ID of the address event
        recorded_by: Peer ID that recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection

    Returns:
        address_id if successful, None otherwise
    """
    log.debug(
        f"address.project() address_id={address_id[:20]}..., "
        f"recorded_by={recorded_by[:20]}..."
    )

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    from db import create_unsafe_db
    unsafedb = create_unsafe_db(db)
    blob = store.get(address_id, unsafedb)
    if not blob:
        log.warning(f"address.project() blob not found for address_id={address_id}")
        return None

    # Parse JSON
    try:
        event_data = json.loads(blob.decode())
    except Exception as e:
        log.warning(f"address.project() failed to parse event data: {e}")
        return None

    # Validate event structure
    if event_data.get('type') != 'network_address':
        log.warning(f"address.project() wrong type: {event_data.get('type')}")
        return None

    observed_peer_id = event_data.get('observed_peer_id')
    observed_by_peer_id = event_data.get('observed_by_peer_id')
    ip = event_data.get('ip')
    port = event_data.get('port')
    created_at = event_data.get('created_at')

    if not all([observed_peer_id, observed_by_peer_id, ip, port, created_at]):
        log.warning(f"address.project() missing required fields")
        return None

    # Insert into network_addresses table
    safedb.execute(
        """INSERT OR REPLACE INTO network_addresses
           (address_id, observed_peer_id, observed_by_peer_id, ip, port, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            address_id,
            observed_peer_id,
            observed_by_peer_id,
            ip,
            port,
            created_at,
            recorded_by,
            recorded_at
        )
    )

    # Mark as valid for this peer
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (address_id, recorded_by)
    )

    log.info(
        f"address.project() inserted: {observed_by_peer_id[:20]}... → "
        f"{observed_peer_id[:20]}... at {ip}:{port}"
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
