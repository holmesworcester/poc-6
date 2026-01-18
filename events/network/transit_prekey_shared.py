"""Transit prekey shared event type (shareable public transit prekey for sync routing)."""

# Registry metadata
EVENT_TYPE = 'transit_prekey_shared'
SHAREABLE = True  # Public transit prekeys sync for routing
PROJECTION_TABLE = ('transit_prekeys_shared', 'transit_prekey_shared_id')

from typing import Any
import logging
from core import crypto
from core import store
from events.identity import peer
from core.db import create_safe_db, create_unsafe_db
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
    """Pure projector for transit_prekey_shared events."""
    event_data = ctx.event_data

    transit_prekey_id = event_data.get('transit_prekey_id')
    peer_id = event_data.get('peer_id')
    public_key_b64 = event_data.get('public_key')
    created_at = event_data.get('created_at')

    if not all([transit_prekey_id, peer_id, public_key_b64, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    public_key = crypto.b64decode(public_key_b64)

    writes = (
        WriteOp(
            op='insert',
            table='transit_prekeys_shared',
            values={
                'transit_prekey_shared_id': ctx.event_id,
                'transit_prekey_id': transit_prekey_id,
                'peer_id': peer_id,
                'public_key': public_key,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(prekey_id: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any) -> str:
    """Create a shareable transit_prekey_shared event from a local transit prekey.

    Args:
        prekey_id: Local transit_prekey event ID (to get public key from)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        transit_prekey_shared_id: The stored transit_prekey_shared event ID
    """
    log.info(f"transit_prekey_shared.create() creating for prekey_id={prekey_id}, t_ms={t_ms}")

    # Get public key from local transit_prekey event
    prekey_blob = store.get(prekey_id, db)
    if not prekey_blob:
        raise ValueError(f"transit_prekey not found: {prekey_id}")

    prekey_data = crypto.parse_json(prekey_blob)
    prekey_public_b64 = prekey_data['public_key']

    # Create shareable event (signed plaintext)
    # Include transit_prekey_id for linking back during projection
    event_data = {
        'type': 'transit_prekey_shared',
        'transit_prekey_id': prekey_id,
        'peer_id': peer_shared_id,  # Public identity (peer_shared_id)
        'public_key': prekey_public_b64,
        'signed_by': peer_shared_id,
        'signer_type': 'peer_shared',  # Required for v2 resolver
        'created_at': t_ms
    }

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext (no encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    transit_prekey_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"transit_prekey_shared.create() created transit_prekey_shared_id={transit_prekey_shared_id}")
    return transit_prekey_shared_id


def project(transit_prekey_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project transit_prekey_shared into transit_prekeys_shared table.

    This makes the public key available for wrapping sync requests.
    """
    log.info(f"transit_prekey_shared.project() transit_prekey_shared_id={transit_prekey_shared_id}, seen_by={recorded_by}")

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(transit_prekey_shared_id, unsafedb)
    if not blob:
        log.warning(f"transit_prekey_shared.project() blob not found")
        return None

    # Parse event data
    event_data = crypto.parse_json(blob)

    # Verify signature using peer_shared public key
    if not crypto.verify_signed_by_peer_shared(event_data, recorded_by, db):
        log.warning(f"transit_prekey_shared.project() signature verification failed for transit_prekey_shared_id={transit_prekey_shared_id}")
        return None

    public_key = crypto.b64decode(event_data['public_key'])

    # Insert into transit_prekeys_shared table
    log.info(f"transit_prekey_shared.project() storing transit_prekey_id={event_data['transit_prekey_id'][:30]}... for peer={event_data['peer_id'][:20]}..., recorded_by={recorded_by[:20]}...")
    safedb.execute(
        """INSERT OR IGNORE INTO transit_prekeys_shared
           (transit_prekey_shared_id, transit_prekey_id, peer_id, public_key, created_at, recorded_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            transit_prekey_shared_id,
            event_data['transit_prekey_id'],
            event_data['peer_id'],
            public_key,
            event_data['created_at'],
            recorded_by
        )
    )

    return transit_prekey_shared_id
