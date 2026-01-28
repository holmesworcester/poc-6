"""Self-announced address event type (peer's own network address for direct communication)."""

# Registry metadata
EVENT_TYPE = 'self_address'
SHAREABLE = True  # Addresses sync to enable peer discovery
PROJECTION_TABLE = ('addresses', 'address_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from events.identity import peer
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)



def _wire_shadow_self_address(peer_shared_id: str, ip: str, port: int) -> None:
    """Validate self_address fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_self_address_plaintext(
        peer_id=crypto.b64decode(peer_shared_id),
        ip=ip,
        port=port,
    )
    decoded = wire_format.decode_self_address_plaintext(plaintext)
    if decoded["ip"] != ip or decoded["port"] != port:
        raise ValueError("wire shadow decode self_address mismatch")

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
    """Pure projector for self_address events."""
    event_data = ctx.event_data

    peer_shared_id = event_data.get('peer_id')  # Note: field is 'peer_id' not 'peer_shared_id'
    ip = event_data.get('ip')
    port = event_data.get('port')
    created_at = event_data.get('created_at')

    if not all([peer_shared_id, ip, port is not None, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_self_address(peer_shared_id, ip, port)

    writes = (
        WriteOp(
            op='insert',
            table='addresses',
            values={
                'address_id': ctx.event_id,
                'peer_shared_id': peer_shared_id,
                'ip': ip,
                'port': port,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


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
    log.info(f"self_address.create() creating address for peer_shared_id={peer_shared_id[:20]}..., ip={ip}, port={port}")

    # Get peer's private key for signing
    private_key = peer.get_private_key(peer_id, peer_id, db)

    _wire_shadow_self_address(peer_shared_id, ip, port)

    blob = wire_format.encode_self_address_wire_event(
        peer_id_b64=peer_shared_id,
        ip=ip,
        port=port,
        created_at_ms=t_ms,
        private_key=private_key,
    )
    address_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"self_address.create() created address_id={address_id[:20]}...")
    return address_id


def get_latest(peer_shared_id: str, recorded_by: str, db: Any) -> tuple[str, int] | None:
    """Get the most recent self-announced address for a peer.

    Args:
        peer_shared_id: The peer's shared ID
        recorded_by: Local peer ID doing the lookup
        db: Database connection

    Returns:
        (ip, port) tuple or None if no addresses known
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    row = safedb.query_one(
        """SELECT ip, port FROM addresses
           WHERE peer_shared_id = ? AND recorded_by = ?
           ORDER BY created_at DESC LIMIT 1""",
        (peer_shared_id, recorded_by)
    )

    if row:
        return (row['ip'], row['port'])
    return None


def announce_for_all_peers(t_ms: int, db: Any) -> dict[str, Any]:
    """Announce self-address for all local peers.

    Checks current address from network layer and announces if changed
    or if no previous announcement exists.

    Args:
        t_ms: Current timestamp in milliseconds
        db: Database connection

    Returns:
        Dict with stats: announced (count), skipped (count), errors (list)
    """
    from core.db import create_unsafe_db

    stats = {'announced': 0, 'skipped': 0, 'errors': []}

    # Get network engine if available
    try:
        from simulator import network
        engine = network.get_engine()
    except ImportError:
        engine = None

    unsafedb = create_unsafe_db(db)
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']

        # Get peer_shared_id
        safedb = create_safe_db(db, recorded_by=peer_id)
        peer_self_row = safedb.query_one(
            "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
            (peer_id, peer_id)
        )

        if not peer_self_row:
            log.debug(f"self_address.announce: skipping peer {peer_id[:20]}... (no peer_shared_id)")
            stats['skipped'] += 1
            continue

        peer_shared_id = peer_self_row['peer_shared_id']

        # Get current address from network layer
        if engine:
            endpoint = engine.get_endpoint(peer_id)
            if endpoint:
                current_ip = endpoint.public_ip
                current_port = endpoint.public_port
            else:
                log.debug(f"self_address.announce: skipping peer {peer_id[:20]}... (no endpoint)")
                stats['skipped'] += 1
                continue
        else:
            # No network layer - skip
            log.debug(f"self_address.announce: skipping peer {peer_id[:20]}... (no network layer)")
            stats['skipped'] += 1
            continue

        # Check if address changed
        latest = get_latest(peer_shared_id, peer_id, db)
        if latest and latest[0] == current_ip and latest[1] == current_port:
            log.debug(f"self_address.announce: skipping peer {peer_id[:20]}... (address unchanged)")
            stats['skipped'] += 1
            continue

        # Announce new address
        try:
            address_id = create(peer_id, peer_shared_id, current_ip, current_port, t_ms, db)
            project(address_id, peer_id, t_ms, db)
            stats['announced'] += 1
            log.info(f"self_address.announce: announced {current_ip}:{current_port} for peer {peer_id[:20]}...")
        except Exception as e:
            stats['errors'].append(f"peer {peer_id[:20]}...: {e}")
            log.warning(f"self_address.announce: error for peer {peer_id[:20]}...: {e}")

    return stats
