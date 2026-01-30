"""Connection pubkey shared event type (shareable public connection pubkey for sync routing).

Renamed from connection_prekey_shared for naming consistency (prekey → pubkey).
"""

# Registry metadata
EVENT_TYPE = 'connection_pubkey_shared'
SHAREABLE = True  # Public connection pubkeys sync for routing
PROJECTION_TABLE = ('connection_pubkeys_shared', 'connection_pubkey_shared_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from events.identity import peer
from core.projection.types import ProjectorResult, WriteOp

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
    """Pure projector for connection_pubkey_shared events."""
    event_data = ctx.event_data

    connection_pubkey_id = event_data.get('connection_pubkey_id')
    peer_id = event_data.get('peer_id')
    public_key_b64 = event_data.get('public_key')
    created_at = event_data.get('created_at')

    if not all([connection_pubkey_id, peer_id, public_key_b64, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_connection_pubkey_shared(connection_pubkey_id, peer_id, public_key_b64)

    public_key = crypto.b64decode(public_key_b64)

    writes = (
        WriteOp(
            op='insert',
            table='connection_pubkeys_shared',
            values={
                'connection_pubkey_shared_id': ctx.event_id,
                'connection_pubkey_id': connection_pubkey_id,
                'peer_id': peer_id,
                'public_key': public_key,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_connection_pubkey_shared(
    connection_pubkey_id: str,
    peer_id: str,
    public_key_b64: str,
) -> None:
    """Validate connection_pubkey_shared fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_connection_pubkey_shared_plaintext(
        connection_pubkey_id=crypto.b64decode(connection_pubkey_id),
        peer_id=crypto.b64decode(peer_id),
        public_key=crypto.b64decode(public_key_b64),
    )
    decoded = wire_format.decode_connection_pubkey_shared_plaintext(plaintext)
    if decoded["connection_pubkey_id"] != crypto.b64decode(connection_pubkey_id):
        raise ValueError("wire shadow decode connection_pubkey_id mismatch")


def create(pubkey_id: str, peer_id: str, peer_shared_id: str, t_ms: int, db: Any) -> str:
    """Create a shareable connection_pubkey_shared event from a local connection pubkey.

    Args:
        pubkey_id: Local connection_pubkey event ID (to get public key from)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection

    Returns:
        connection_pubkey_shared_id: The stored connection_pubkey_shared event ID
    """
    log.info(f"connection_pubkey_shared.create() creating for pubkey_id={pubkey_id}, t_ms={t_ms}")

    # Get public key from local connection_pubkey event
    pubkey_blob = store.get(pubkey_id, db)
    if not pubkey_blob:
        raise ValueError(f"connection_pubkey not found: {pubkey_id}")

    if not wire_format.is_wire_connection_pubkey_envelope(pubkey_blob):
        raise ValueError("connection_pubkey must be wire format")
    pubkey_data = wire_format.decode_connection_pubkey_wire_event(pubkey_blob)
    pubkey_public_b64 = pubkey_data['public_key']

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)

    blob = wire_format.encode_connection_pubkey_shared_wire_event(
        connection_pubkey_id_b64=pubkey_id,
        peer_id_b64=peer_shared_id,
        public_key_b64=pubkey_public_b64,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )

    # Store event with recorded wrapper and projection
    connection_pubkey_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"connection_pubkey_shared.create() created connection_pubkey_shared_id={connection_pubkey_shared_id}")
    return connection_pubkey_shared_id
