"""Address event type (peer's network address for direct communication)."""
from typing import Any
import logging
import crypto
import store
from events.identity import peer
from db import create_safe_db

log = logging.getLogger(__name__)


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
        'created_by': peer_shared_id,
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


def project(address_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project address event into addresses table.

    Args:
        address_id: Event ID of the address event
        recorded_by: Peer ID that recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection

    Returns:
        address_id if successful, None otherwise
    """
    log.info(f"address.project() address_id={address_id[:20]}..., recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    from db import create_unsafe_db
    unsafedb = create_unsafe_db(db)
    blob = store.get(address_id, unsafedb)
    if not blob:
        log.warning(f"address.project() blob not found for address_id={address_id}")
        return None

    # Parse JSON (signed plaintext)
    event_data = crypto.parse_json(blob)

    # Verify signature using public key from peer_shared
    peer_shared_id = event_data.get('peer_id')
    if not peer_shared_id:
        log.warning(f"address.project() missing peer_id in event data")
        return None

    # Get public key from peers_shared table
    from events.identity import peer_shared as peer_shared_module
    public_key = peer_shared_module.get_public_key(peer_shared_id, recorded_by, db)
    if not public_key:
        log.warning(f"address.project() could not get public key for peer_shared_id={peer_shared_id}")
        return None

    if not crypto.verify_event(event_data, public_key):
        log.warning(f"address.project() signature verification failed for address_id={address_id}")
        return None

    # Insert into addresses table
    safedb.execute(
        """INSERT OR REPLACE INTO addresses
           (address_id, peer_shared_id, ip, port, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            address_id,
            peer_shared_id,
            event_data.get('ip'),
            event_data.get('port'),
            event_data.get('created_at'),
            recorded_by,
            recorded_at
        )
    )
    log.info(f"address.project() inserted into addresses: address_id={address_id[:20]}..., peer_shared_id={peer_shared_id[:20]}...")

    # Mark as valid for this peer
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (address_id, recorded_by)
    )

    return address_id
