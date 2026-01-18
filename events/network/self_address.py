"""Self-announced address event type (peer's own network address for direct communication)."""

# Registry metadata
EVENT_TYPE = 'self_address'
SHAREABLE = True  # Addresses sync to enable peer discovery
PROJECTION_TABLE = ('addresses', 'address_id')

from typing import Any
import logging
from core import crypto
from core import store
from events.identity import peer
from core.db import create_safe_db
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


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

    # Create event dict (plaintext, will be signed)
    event_data = {
        'type': 'self_address',
        'peer_id': peer_shared_id,
        'signed_by': peer_shared_id,
        'signer_type': 'peer_shared',  # Required for v2 resolver
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

    log.info(f"self_address.create() created address_id={address_id[:20]}...")
    return address_id


def project(address_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project self_address event into addresses table.

    Args:
        address_id: Event ID of the address event
        recorded_by: Peer ID that recorded this event
        recorded_at: Timestamp when recorded
        db: Database connection

    Returns:
        address_id if successful, None otherwise
    """
    log.info(f"self_address.project() address_id={address_id[:20]}..., recorded_by={recorded_by[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    from core.db import create_unsafe_db
    unsafedb = create_unsafe_db(db)
    blob = store.get(address_id, unsafedb)
    if not blob:
        log.warning(f"self_address.project() blob not found for address_id={address_id}")
        return None

    # Parse JSON (signed plaintext)
    event_data = crypto.parse_json(blob)

    # Verify signature using public key from peer_shared
    peer_shared_id = event_data.get('peer_id')
    if not peer_shared_id:
        log.warning(f"self_address.project() missing peer_id in event data")
        return None

    # Get public key from peers_shared table
    from events.identity import peer_shared as peer_shared_module
    public_key = peer_shared_module.get_public_key(peer_shared_id, recorded_by, db)
    if not public_key:
        log.warning(f"self_address.project() could not get public key for peer_shared_id={peer_shared_id}")
        return None

    if not crypto.verify_event(event_data, public_key):
        log.warning(f"self_address.project() signature verification failed for address_id={address_id}")
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
    log.info(f"self_address.project() inserted into addresses: address_id={address_id[:20]}..., peer_shared_id={peer_shared_id[:20]}...")

    # Mark as valid for this peer
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (address_id, recorded_by)
    )

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
