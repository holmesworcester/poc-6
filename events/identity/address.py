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


def project(address_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project address event into addresses table."""
    from projectors import resolve
    from projectors import address as addr_projector

    input_dict = resolve("address", address_id, recorded_by, recorded_at, db)
    if not input_dict:
        return None

    result = addr_projector.project(input_dict)

    if not result.valid:
        log.warning(f"address.project() failed: {result.reason}")
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
