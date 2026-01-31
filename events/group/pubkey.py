"""TreeKEM pubkey event type (local-only keypair storage for sender key distribution).

This is the LOCAL event that stores the keypair. The public key is shared
via pubkey_shared events. This pattern ensures private keys survive replay
since local events are not affected by project_pure() during replay.

Pattern: pubkey (local, SHAREABLE=False) + pubkey_shared (shareable, SHAREABLE=True)
Similar to: group_prekey + group_prekey_shared
"""

# Registry metadata
EVENT_TYPE = 'pubkey'
SHAREABLE = False  # Local-only - contains private key material
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# v2 event specification - no signer, no deps (local-only deterministic event)
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for pubkey events.

    Pubkeys are local-only deterministic events (created_at=0 in blob).
    Uses recorded_at as created_at and recorded_by as owner_peer_id.
    Stores both public and private key in the pubkeys table.
    """
    event_data = ctx.event_data

    public_key_b64 = event_data.get('public_key')
    private_key_b64 = event_data.get('private_key')

    if not public_key_b64 or not private_key_b64:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Validate and decode keys
    try:
        public_key = crypto.b64decode(public_key_b64)
        private_key = crypto.b64decode(private_key_b64)
        if len(public_key) != 32 or len(private_key) != 32:
            return ProjectorResult(writes=tuple(), valid_event=False)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Use recorded_at as created_at (deterministic blobs have no timestamp)
    created_at = ctx.recorded_at

    writes = (
        WriteOp(
            op='insert',
            table='pubkeys',
            values={
                'pubkey_id': ctx.event_id,
                'owner_peer_id': ctx.recorded_by,
                'public_key': public_key,
                'private_key': private_key,
                'created_at': created_at,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a new local pubkey event with a fresh keypair.

    This creates the LOCAL event that stores the keypair. To share the public
    key with other peers, also create a pubkey_shared event.

    Args:
        peer_id: Local peer ID (owner of this pubkey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (pubkey_id, private_key): The pubkey event ID and private key bytes
    """
    log.info(f"pubkey.create() creating new pubkey for peer_id={peer_id}, t_ms={t_ms}")

    # Generate Ed25519 keypair
    private_key, public_key = crypto.generate_keypair()

    # Create DETERMINISTIC event blob - only type and keys, created_at=0
    # This ensures same key material = same pubkey_id on all peers
    blob = wire_format.encode_pubkey_local_event(
        public_key=public_key,
        private_key=private_key,
    )

    # Store event
    pubkey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"pubkey.create() created pubkey_id={pubkey_id}")

    return pubkey_id, private_key


def create_from_material(public_key: bytes, private_key: bytes, peer_id: str,
                         t_ms: int, db: Any) -> str:
    """Create pubkey event from provided key material.

    Creates a DETERMINISTIC pubkey event from the key material.
    Same key material = same pubkey_id on all peers.

    Args:
        public_key: Ed25519 public key bytes
        private_key: Ed25519 private key bytes
        peer_id: Peer ID that owns this pubkey
        t_ms: Timestamp (used for recorded_at, NOT in the blob)
        db: Database connection

    Returns:
        pubkey_id: The deterministic event ID
    """
    log.info(f"pubkey.create_from_material() creating pubkey for peer_id={peer_id}, t_ms={t_ms}")

    # Create DETERMINISTIC event blob
    blob = wire_format.encode_pubkey_local_event(
        public_key=public_key,
        private_key=private_key,
    )

    # Store event
    pubkey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"pubkey.create_from_material() created pubkey_id={pubkey_id}")

    return pubkey_id


def get_private_key(pubkey_id: str, recorded_by: str, db: Any) -> bytes | None:
    """Get the private key for a pubkey we own.

    Args:
        pubkey_id: The pubkey event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Private key bytes or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT private_key FROM pubkeys WHERE pubkey_id = ? AND recorded_by = ?",
        (pubkey_id, recorded_by)
    )
    return row['private_key'] if row else None


def get_public_key(pubkey_id: str, recorded_by: str, db: Any) -> bytes | None:
    """Get the public key for a pubkey.

    Args:
        pubkey_id: The pubkey event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Public key bytes or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT public_key FROM pubkeys WHERE pubkey_id = ? AND recorded_by = ?",
        (pubkey_id, recorded_by)
    )
    return row['public_key'] if row else None


def get_pubkey_by_id(pubkey_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get pubkey data by ID.

    This looks up a pubkey by its ID. It first checks the local pubkeys table
    (for keys we own with private key), then falls back to pubkeys_shared
    (for other peers' public keys).

    Args:
        pubkey_id: The pubkey ID to look up
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'type': 'asymmetric'}
        or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # First try local pubkeys (we own these and have private key)
    row = safedb.query_one(
        "SELECT pubkey_id, public_key FROM pubkeys WHERE pubkey_id = ? AND recorded_by = ?",
        (pubkey_id, recorded_by)
    )
    if row:
        return {
            'id': crypto.b64decode(row['pubkey_id']),
            'public_key': row['public_key'],
            'type': 'asymmetric',
        }

    # Fall back to pubkeys_shared (other peers' public keys)
    row = safedb.query_one(
        "SELECT pubkey_id, public_key FROM pubkeys_shared WHERE pubkey_id = ? AND recorded_by = ?",
        (pubkey_id, recorded_by)
    )
    if row:
        return {
            'id': crypto.b64decode(row['pubkey_id']),
            'public_key': row['public_key'],
            'type': 'asymmetric',
        }

    return None


def list_own_pubkeys(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all pubkeys owned by this peer.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of pubkey records with pubkey_id, public_key, created_at
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT pubkey_id, public_key, created_at
           FROM pubkeys WHERE owner_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id, peer_id)
    ))
