"""Observed address event type (peer-announced endpoint observations).

A peer announces observations about another peer's public endpoint.
This supports Byzantine fault tolerance: multiple peers can attest to the same address.

Usage:
  - Alice observes Bob at 203.0.113.5:42000 (from Bob's sync packet)
  - Alice creates address event announcing this observation
  - Charlie receives Alice's observation and learns Bob's endpoint
"""

# Registry metadata
EVENT_TYPE = 'observed_address'
SHAREABLE = True  # Address observations sync for peer discovery
PROJECTION_TABLE = None

from typing import Any, Optional
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)



def _wire_shadow_observed_address(observed_peer_id: str, ip: str, port: int) -> None:
    """Validate observed_address fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_observed_address_plaintext(
        observed_peer_id=crypto.b64decode(observed_peer_id),
        ip=ip,
        port=port,
    )
    decoded = wire_format.decode_observed_address_plaintext(plaintext)
    if decoded["ip"] != ip or decoded["port"] != port:
        raise ValueError("wire shadow decode observed_address mismatch")

# v2 event specification - signed by peer_shared, no deps
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for observed_address events.

    Observed addresses are signed plaintext events recording peer endpoints.
    """
    event_data = ctx.event_data

    observed_peer_id = event_data.get('observed_peer_id')
    signed_by = event_data.get('signed_by')
    ip = event_data.get('ip')
    port = event_data.get('port')
    created_at = event_data.get('created_at')

    if not all([observed_peer_id, signed_by, ip, port is not None, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_observed_address(observed_peer_id, ip, port)

    writes = (
        WriteOp(
            op='insert',
            table='network_addresses',
            values={
                'address_id': ctx.event_id,
                'observed_peer_id': observed_peer_id,
                'signed_by': signed_by,
                'ip': ip,
                'port': port,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(
    observed_peer_id: str,
    peer_id: str,
    peer_shared_id: str,
    ip: str,
    port: int,
    t_ms: int,
    db: Any
) -> str:
    """Create address event announcing observation of a peer's endpoint.

    Args:
        observed_peer_id: The peer whose endpoint was observed
        peer_id: Local peer_id of the observer (for signing/storage)
        peer_shared_id: peer_shared_id of the observer (for signed_by field)
        ip: Observed IP address
        port: Observed port number
        t_ms: Timestamp
        db: Database connection

    Returns:
        address_id: Event ID of the created address event
    """
    log.info(
        f"observed_address.create() {peer_shared_id[:20]}... observed "
        f"{observed_peer_id[:20]}... at {ip}:{port}"
    )

    # Sign the event
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)
    _wire_shadow_observed_address(observed_peer_id, ip, port)

    blob = wire_format.encode_observed_address_wire_event(
        observed_peer_id_b64=observed_peer_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        ip=ip,
        port=port,
        created_at_ms=t_ms,
        private_key=private_key,
    )
    address_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"observed_address.create() created address_id={address_id[:20]}...")
    return address_id


def get_addresses(peer_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """Get all known addresses for a peer from local perspective.

    Args:
        peer_id: The peer whose addresses to look up
        recorded_by: Local peer ID doing the lookup
        db: Database connection

    Returns:
        List of dicts with keys: address_id, ip, port, signed_by, created_at
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    rows = safedb.query(
        """SELECT address_id, ip, port, signed_by, created_at
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
            'signed_by': row['signed_by'],
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
