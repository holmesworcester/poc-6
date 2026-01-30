"""Network name update event type (encrypted name update for networks).

In sender key model, network_name_update is encrypted with sender keys to the main group.
"""

# Registry metadata
EVENT_TYPE = 'network_name_update'
SHAREABLE = True  # Name updates sync across network
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from events.group import group, sender_key
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db
from core.projection.types import ProjectorResult, WriteOp

EVENT_SPEC = {
    'encrypted': True,  # Encrypted with sender keys
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {
        'network': {
            'source': 'table',
            'table': 'networks',
            'key': 'network_id',
            'fields': ['network_id'],
        },
    },
    'optional': {
        'existing_name': {
            'source': 'table',
            'table': 'network_names',
            'key': 'network_id',
            'key_from': 'network_id',
            'fields': ['network_id', 'global_count'],
        },
    },
    'cascade_on_delete': [],
}

log = logging.getLogger(__name__)



def _wire_shadow_network_name_update(network_id: str, name: str) -> None:
    """Validate network_name_update fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_network_name_update_plaintext(
        network_id=crypto.b64decode(network_id),
        name=name,
    )
    decoded = wire_format.decode_network_name_update_plaintext(plaintext)
    if decoded["name"] != name:
        raise ValueError("wire shadow decode name mismatch")


def create(network_id: str, name: str, peer_id: str, peer_shared_id: str, t_ms: int,
           db: Any) -> str:
    """Create a network_name_update event.

    In sender key model, network_name_update is encrypted with sender keys to the main group.

    Args:
        network_id: The network event ID this name updates
        name: Network name
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        network_name_update_id: The stored event ID

    Raises:
        KeyNotAvailableError: If main group not available yet
    """
    log.info(f"network_name_update.create() creating network name for network_id={network_id[:20]}..., name='{name}'")

    # Get main group (all_members) - use is_main flag since name varies
    main_group = group.get_main(peer_id, db)
    if not main_group:
        log.info(f"network_name_update.create() main group not found yet")
        raise KeyNotAvailableError("Main group not available yet - will be created on sync")

    group_id = main_group['group_id']

    # Get or create sender key for main group
    key_data = sender_key.pick_or_create_key(
        group_id=group_id,
        peer_id=peer_id,
        peer_shared_id=peer_shared_id,
        t_ms=t_ms,
        db=db,
    )

    _wire_shadow_network_name_update(network_id, name)

    private_key = peer.get_private_key(peer_id, peer_id, db)
    blob = wire_format.encode_network_name_update_wire_event(
        network_id_b64=network_id,
        name=name,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        global_count=0,
        created_at_ms=t_ms,
        key_data=key_data,
        private_key=private_key,
    )
    network_name_update_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"network_name_update.create() created network_name_update_id={network_name_update_id[:20]}...")
    return network_name_update_id


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for network_name_update events."""
    event_data = ctx.event_data

    if event_data.get('type') != 'network_name_update':
        return ProjectorResult(writes=tuple(), valid_event=False)

    network_id = event_data.get('network_id')
    name = event_data.get('name')
    key_id = event_data.get('key_id')
    global_count = event_data.get('global_count', 0)

    if not network_id or not name:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_network_name_update(network_id, name)

    existing = ctx.deps.get('existing_name')
    writes: list[WriteOp] = []

    if existing is None:
        writes.append(
            WriteOp(
                op='insert',
                table='network_names',
                values={
                    'network_id': network_id,
                    'name': name,
                    'event_id': ctx.event_id,
                    'global_count': global_count,
                    'key_id': key_id,
                    'created_at': event_data.get('created_at'),
                    'signed_by': event_data.get('signed_by'),
                    'recorded_at': ctx.recorded_at,
                },
            )
        )
    elif existing.get('global_count', 0) < global_count:
        writes.append(
            WriteOp(
                op='update',
                table='network_names',
                values={
                    'name': name,
                    'event_id': ctx.event_id,
                    'global_count': global_count,
                    'key_id': key_id,
                    'created_at': event_data.get('created_at'),
                    'signed_by': event_data.get('signed_by'),
                    'recorded_at': ctx.recorded_at,
                },
                where={
                    'network_id': network_id,
                },
            )
        )

    return ProjectorResult(writes=tuple(writes), valid_event=True)


def validate(event_id: str, recorded_by: str, db: Any) -> str | None:
    """Validate a network_name_update event.

    Validation checks:
    1. Does the network event (with event_id = network_id field) exist?

    Args:
        event_id: The network_name_update event ID
        recorded_by: The peer that recorded this event
        db: Database connection

    Returns:
        'VALID' if network exists and valid
        'BLOCKED' if network doesn't exist yet (wait for network event)
        None if invalid (can't recover)
    """
    log.debug(f"network_name_update.validate() validating {event_id[:20]}...")

    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get the event blob to extract network_id field
    blob = store.get(event_id, db)
    if not blob:
        log.warning(f"network_name_update.validate() blob not found for {event_id[:20]}...")
        return None

    try:
        if not wire_format.is_wire_network_name_update_envelope(blob):
            log.warning(f"network_name_update.validate() non-wire event blob for {event_id[:20]}...")
            return None
        event_data, missing = wire_format.decode_network_name_update_wire_event(blob, recorded_by, db)
        if not event_data:
            return 'BLOCKED' if missing else None
    except Exception as e:
        log.warning(f"network_name_update.validate() failed to parse event: {e}")
        return None

    # Extract network_id field
    network_id = event_data.get('network_id')
    if not network_id:
        log.warning(f"network_name_update.validate() missing network_id field")
        return None

    # Check: Does the network event exist?
    network_event = safedb.query_one(
        "SELECT network_id FROM networks WHERE network_id = ? AND recorded_by = ? LIMIT 1",
        (network_id, recorded_by)
    )

    if not network_event:
        log.debug(f"network_name_update.validate() network not found, blocking: {network_id[:20]}...")
        return "BLOCKED"

    log.debug(f"network_name_update.validate() valid: network exists")
    return "VALID"


class KeyNotAvailableError(Exception):
    """Raised when group key is not available for encryption."""
    pass
