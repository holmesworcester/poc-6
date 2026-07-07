"""Prekey shared event type (shareable public prekey)."""

# Registry metadata
EVENT_TYPE = 'group_prekey_shared'
SHAREABLE = True  # Public prekeys sync for key sealing
PROJECTION_TABLE = ('group_prekeys_shared', 'group_prekey_shared_id')

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
    """Pure projector for group_prekey_shared events."""
    event_data = ctx.event_data

    group_prekey_id = event_data.get('group_prekey_id')
    peer_id = event_data.get('peer_id')
    public_key_b64 = event_data.get('public_key')
    created_at = event_data.get('created_at')

    if not all([group_prekey_id, peer_id, public_key_b64, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

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


def create(prekey_id: str, peer_id: str, peer_shared_id: str,
           t_ms: int, db: Any,
           group_id: str | None = None, key_id: str | None = None,
           user_id: str | None = None,
           wrap_key_data: dict | None = None) -> str:
    """Create a shareable group_prekey_shared event from a local group prekey.

    Context parameters are optional - they are stored in the event for reference
    but are not required for the prekey to function. The prekey can be used for
    encrypting group keys to any member regardless of context.

    Args:
        prekey_id: Local group_prekey event ID (to get public key from)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (for created_by)
        t_ms: Timestamp
        db: Database connection
        group_id: Optional group context (for mode=user invites)
        key_id: Optional key reference (for mode=user invites)
        user_id: Optional user context (for mode=link invites - device linking)
        wrap_key_data: Optional key dict for wrapping (used when network key not available yet)

    Returns:
        group_prekey_shared_id: The stored group_prekey_shared event ID
    """
    # Context parameters are optional - validate consistency if group context provided
    has_group_context = group_id is not None
    has_user_context = user_id is not None

    # If group context is provided, key_id should also be provided (but not required)
    if has_group_context and key_id is None:
        log.warning("group_prekey_shared.create() group_id provided without key_id")

    log.info(f"group_prekey_shared.create() creating group_prekey_shared for prekey_id={prekey_id}, t_ms={t_ms}")

    # Get public key from local prekey event
    prekey_blob = store.get(prekey_id, db)
    if not prekey_blob:
        raise ValueError(f"prekey not found: {prekey_id}")

    prekey_data = crypto.parse_json(prekey_blob)
    prekey_public_b64 = prekey_data['public_key']

    # Create shareable event (encrypted + signed)
    # Include group_prekey_id for linking back during projection
    event_data = {
        'type': 'group_prekey_shared',
        'group_prekey_id': prekey_id,
        'peer_id': peer_shared_id,
        'public_key': prekey_public_b64,
        'signed_by': peer_shared_id,
        'signer_type': 'peer_shared',  # Required for v2 resolver
        'created_at': t_ms,
    }

    # Add context fields if provided (all optional):
    # - mode=user: group context (group_id, key_id)
    # - mode=link: user context (user_id)
    # - no context: standalone prekey for key rotation
    if has_group_context:
        event_data['group_id'] = group_id
        if key_id:
            event_data['key_id'] = key_id
    if has_user_context:
        event_data['user_id'] = user_id

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)
    signed_event = crypto.sign_event(event_data, private_key)

    # Store as signed plaintext (no inner encryption)
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    group_prekey_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"group_prekey_shared.create() created group_prekey_shared_id={group_prekey_shared_id}")
    return group_prekey_shared_id



def project(group_prekey_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project group_prekey_shared event into group_prekeys_shared table and shareable_events."""
    log.info(f"group_prekey_shared.project() group_prekey_shared_id={group_prekey_shared_id}, seen_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(group_prekey_shared_id, unsafedb)
    if not blob:
        log.warning(f"group_prekey_shared.project() blob not found for group_prekey_shared_id={group_prekey_shared_id}")
        return None

    # Parse JSON (signed plaintext, no decryption needed)
    event_data = crypto.parse_json(blob)

    # Verify signature - get public key from signed_by peer_shared
    from events.identity import peer_shared
    signed_by = event_data['signed_by']
    public_key = peer_shared.get_public_key(signed_by, recorded_by, db)
    if not crypto.verify_event(event_data, public_key):
        log.warning(f"group_prekey_shared.project() signature verification failed for group_prekey_shared_id={group_prekey_shared_id}")
        return None

    # Insert into group_prekeys_shared table
    # group_prekey_id is stored for use as crypto hint (matches recipient's local prekey_id)
    prekey_public = crypto.b64decode(event_data['public_key'])
    safedb.execute(
        """INSERT OR IGNORE INTO group_prekeys_shared
           (group_prekey_shared_id, group_prekey_id, peer_id, public_key, created_at, recorded_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            group_prekey_shared_id,
            event_data['group_prekey_id'],
            event_data['peer_id'],
            prekey_public,
            event_data['created_at'],
            recorded_by
        )
    )

    # Mark as valid for this peer
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (group_prekey_shared_id, recorded_by)
    )

    return group_prekey_shared_id
