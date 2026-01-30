"""TreeKEM pubkey_shared event type (shareable public key for sender key distribution).

This is the SHAREABLE event that distributes public keys to other peers.
The keypair is stored in the local pubkey event. This pattern ensures
private keys survive replay.

Pattern: pubkey (local, SHAREABLE=False) + pubkey_shared (shareable, SHAREABLE=True)
Similar to: group_prekey + group_prekey_shared
"""

# Registry metadata
EVENT_TYPE = 'pubkey_shared'
SHAREABLE = True  # Public keys sync for key wrapping
PROJECTION_TABLE = ('pubkeys_shared', 'pubkey_shared_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from events.identity import peer
from core.db import create_safe_db
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
    """Pure projector for pubkey_shared events.

    pubkey_shared events share public keys with other peers for sender key
    distribution. Recipients can use these to encrypt secrets to the owner.
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'pubkey_shared':
        return ProjectorResult(writes=tuple(), valid_event=False)

    pubkey_id = event_data.get('pubkey_id')
    owner_peer_id = event_data.get('owner_peer_id')
    public_key_b64 = event_data.get('public_key')
    created_at = event_data.get('created_at')

    if not all([pubkey_id, owner_peer_id, public_key_b64, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Validate public key
    try:
        public_key = crypto.b64decode(public_key_b64)
        if len(public_key) != 32:
            return ProjectorResult(writes=tuple(), valid_event=False)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='pubkeys_shared',
            values={
                'pubkey_shared_id': ctx.event_id,
                'pubkey_id': pubkey_id,
                'owner_peer_id': owner_peer_id,
                'public_key': public_key,
                'created_at': created_at,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(pubkey_id: str, peer_id: str, peer_shared_id: str,
           t_ms: int, db: Any) -> str:
    """Create a shareable pubkey_shared event from a local pubkey.

    Args:
        pubkey_id: Local pubkey event ID (to get public key from)
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (owner identity)
        t_ms: Timestamp
        db: Database connection

    Returns:
        pubkey_shared_id: The stored pubkey_shared event ID
    """
    log.info(f"pubkey_shared.create() creating pubkey_shared for pubkey_id={pubkey_id[:20]}..., t_ms={t_ms}")

    # Get public key from local pubkey event
    from events.group import pubkey
    public_key = pubkey.get_public_key(pubkey_id, peer_id, db)
    if not public_key:
        raise ValueError(f"pubkey not found: {pubkey_id}")

    # Sign the event with local peer's private key
    private_key = peer.get_private_key(peer_id, peer_id, db)

    blob = wire_format.encode_pubkey_shared_wire_event(
        pubkey_id_b64=pubkey_id,
        owner_peer_id_b64=peer_shared_id,
        public_key=public_key,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )

    # Store event with recorded wrapper and projection
    pubkey_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"pubkey_shared.create() created pubkey_shared_id={pubkey_shared_id[:20]}...")
    return pubkey_shared_id


def get_pubkey_for_peer(peer_shared_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the most recent pubkey for a peer.

    This queries the pubkeys_shared table to find OTHER peers' public keys
    for encryption during sender key distribution.

    Args:
        peer_shared_id: Peer's public ID
        recorded_by: Local peer ID requesting
        db: Database connection

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'type': 'asymmetric'}
        or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    result = safedb.query_one(
        """SELECT pubkey_id, public_key FROM pubkeys_shared
           WHERE owner_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC LIMIT 1""",
        (peer_shared_id, recorded_by)
    )

    if not result:
        return None

    return {
        'id': crypto.b64decode(result['pubkey_id']),
        'public_key': result['public_key'],
        'type': 'asymmetric'
    }


def get_pubkey_by_id(pubkey_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get a pubkey by its ID from the shared table.

    Args:
        pubkey_id: The pubkey event ID (references the local pubkey)
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Dict with 'public_key' bytes or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT public_key FROM pubkeys_shared WHERE pubkey_id = ? AND recorded_by = ?",
        (pubkey_id, recorded_by)
    )
    if not row:
        return None
    return {'public_key': row['public_key']}


def list_for_peer(peer_shared_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all shared pubkeys for a peer.

    Args:
        peer_shared_id: Peer's public ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of pubkey records
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return list(safedb.query(
        """SELECT pubkey_shared_id, pubkey_id, public_key, created_at
           FROM pubkeys_shared WHERE owner_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_shared_id, recorded_by)
    ))
