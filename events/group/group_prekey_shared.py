"""Prekey shared event type (shareable public prekey)."""

# Registry metadata
EVENT_TYPE = 'group_prekey_shared'
SHAREABLE = True  # Public prekeys sync for key sealing
PROJECTION_TABLE = ('group_prekeys_shared', 'group_prekey_shared_id')

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
    """Pure projector for group_prekey_shared events."""
    event_data = ctx.event_data

    group_prekey_id = event_data.get('group_prekey_id')
    peer_id = event_data.get('peer_id')
    public_key_b64 = event_data.get('public_key')
    created_at = event_data.get('created_at')

    if not all([group_prekey_id, peer_id, public_key_b64, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_group_prekey_shared(group_prekey_id, peer_id, public_key_b64)

    public_key = crypto.b64decode(public_key_b64)

    writes = (
        WriteOp(
            op='insert',
            table='group_prekeys_shared',
            values={
                'group_prekey_shared_id': ctx.event_id,
                'group_prekey_id': group_prekey_id,
                'peer_id': peer_id,
                'public_key': public_key,
                'created_at': created_at,
                'recorded_by': ctx.recorded_by,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_group_prekey_shared(group_prekey_id: str, peer_id: str, public_key_b64: str) -> None:
    """Validate group_prekey_shared fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_group_prekey_shared_plaintext(
        group_prekey_id=crypto.b64decode(group_prekey_id),
        peer_id=crypto.b64decode(peer_id),
        public_key=crypto.b64decode(public_key_b64),
    )
    decoded = wire_format.decode_group_prekey_shared_plaintext(plaintext)
    if decoded["group_prekey_id"] != crypto.b64decode(group_prekey_id):
        raise ValueError("wire shadow decode group_prekey_id mismatch")


def create(prekey_id: str, peer_id: str, peer_shared_id: str,
           t_ms: int, db: Any,
           group_id: str | None = None, key_id: str | None = None,
           user_id: str | None = None,
           wrap_key_data: dict | None = None) -> str:
    """Create a shareable group_prekey_shared event from a local group prekey.

    Context must be either group-based (for user invites) or user-based (for link invites).
    Exactly one context type must be provided.

    Args:
        prekey_id: Local group_prekey event ID (to get public key from)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection
        group_id: Group context (for mode=user invites). Requires key_id.
        key_id: Key reference (for mode=user invites). Requires group_id.
        user_id: User context (for mode=link invites - device linking)
        wrap_key_data: Optional key dict for wrapping (used when network key not available yet)

    Returns:
        group_prekey_shared_id: The stored group_prekey_shared event ID

    Raises:
        ValueError: If neither group context nor user context is provided,
                    or if both are provided
    """
    # Validate context - must have exactly one type
    has_group_context = group_id is not None
    has_user_context = user_id is not None

    if not has_group_context and not has_user_context:
        raise ValueError("group_prekey_shared requires either group context (group_id, key_id) or user context (user_id)")
    if has_group_context and has_user_context:
        raise ValueError("group_prekey_shared cannot have both group context and user context")
    if has_group_context and key_id is None:
        raise ValueError("group context requires both group_id and key_id")

    log.info(f"group_prekey_shared.create() creating group_prekey_shared for prekey_id={prekey_id}, t_ms={t_ms}")

    # Get public key from local prekey event
    prekey_blob = store.get(prekey_id, db)
    if not prekey_blob:
        raise ValueError(f"prekey not found: {prekey_id}")

    if not wire_format.is_wire_group_prekey_envelope(prekey_blob):
        raise ValueError("group_prekey_shared requires wire group_prekey event")
    prekey_data = wire_format.decode_group_prekey_wire_event(prekey_blob)
    prekey_public_b64 = prekey_data['public_key']

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)

    blob = wire_format.encode_group_prekey_shared_wire_event(
        group_prekey_id_b64=prekey_id,
        peer_id_b64=peer_shared_id,
        public_key_b64=prekey_public_b64,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )

    # Store event with recorded wrapper and projection
    group_prekey_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_prekey_shared.create() created group_prekey_shared_id={group_prekey_shared_id}")
    return group_prekey_shared_id
