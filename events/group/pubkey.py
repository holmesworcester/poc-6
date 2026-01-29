"""TreeKEM pubkey event type (shareable public key for TreeKEM key distribution).

This replaces group_prekey for the TreeKEM O(log n) messaging system.
Pubkeys are signed, shareable events that advertise public keys for key wrapping.
"""

# Registry metadata
EVENT_TYPE = 'pubkey'
SHAREABLE = True  # Syncs to peers
PROJECTION_TABLE = ('pubkeys', 'pubkey_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Event specification - signed, shareable
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
    """Pure projector for pubkey events.

    Pubkeys are signed events that advertise public keys for TreeKEM key distribution.
    """
    event_data = ctx.event_data

    public_key_b64 = event_data.get('public_key')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')

    if not public_key_b64 or not signed_by or created_at is None:
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
            table='pubkeys',
            values={
                'pubkey_id': ctx.event_id,
                'public_key': public_key,
                'owner_peer_id': signed_by,
                'created_at': created_at,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(peer_id: str, peer_shared_id: str, t_ms: int, db: Any,
           removal_epoch_id: str | None = None) -> tuple[str, bytes]:
    """Create a new pubkey event with a fresh keypair.

    Args:
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (signer identity)
        t_ms: Timestamp
        db: Database connection
        removal_epoch_id: Optional removal epoch this pubkey is for

    Returns:
        (pubkey_id, private_key): The pubkey event ID and private key bytes
    """
    log.info(f"pubkey.create() creating new pubkey for peer_id={peer_id}, t_ms={t_ms}")

    # Generate Ed25519 keypair
    private_key, public_key = crypto.generate_keypair()

    # Get private key for signing
    from events.identity import peer
    signing_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wire event
    blob = wire_format.encode_pubkey_wire_event(
        public_key=public_key,
        removal_epoch_id_b64=removal_epoch_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=signing_key,
    )

    # Store event
    pubkey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"pubkey.create() created pubkey_id={pubkey_id}")

    # Store the private key locally (not synced)
    safedb = create_safe_db(db, recorded_by=peer_id)
    safedb.execute(
        """INSERT INTO pubkey_secrets (pubkey_id, private_key, recorded_by)
           VALUES (?, ?, ?)""",
        (pubkey_id, private_key, peer_id)
    )

    return pubkey_id, private_key


def create_from_material(public_key: bytes, private_key: bytes, peer_id: str,
                         peer_shared_id: str, t_ms: int, db: Any,
                         removal_epoch_id: str | None = None) -> str:
    """Create pubkey event from provided key material.

    Args:
        public_key: Ed25519 public key bytes
        private_key: Ed25519 private key bytes
        peer_id: Local peer ID
        peer_shared_id: Public peer ID
        t_ms: Timestamp
        db: Database connection
        removal_epoch_id: Optional removal epoch this pubkey is for

    Returns:
        pubkey_id: The event ID
    """
    log.info(f"pubkey.create_from_material() creating pubkey for peer_id={peer_id}, t_ms={t_ms}")

    # Get private key for signing
    from events.identity import peer
    signing_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wire event
    blob = wire_format.encode_pubkey_wire_event(
        public_key=public_key,
        removal_epoch_id_b64=removal_epoch_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=signing_key,
    )

    # Store event
    pubkey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"pubkey.create_from_material() created pubkey_id={pubkey_id}")

    # Store the private key locally (not synced)
    safedb = create_safe_db(db, recorded_by=peer_id)
    safedb.execute(
        """INSERT INTO pubkey_secrets (pubkey_id, private_key, recorded_by)
           VALUES (?, ?, ?)""",
        (pubkey_id, private_key, peer_id)
    )

    return pubkey_id


def get_pubkey_for_peer(peer_shared_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the most recent pubkey for a peer.

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
        """SELECT pubkey_id, public_key FROM pubkeys
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
        "SELECT private_key FROM pubkey_secrets WHERE pubkey_id = ? AND recorded_by = ?",
        (pubkey_id, recorded_by)
    )
    return row['private_key'] if row else None


def list_for_peer(peer_shared_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """List all pubkeys for a peer.

    Args:
        peer_shared_id: Peer's public ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of pubkey records
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return list(safedb.query(
        """SELECT pubkey_id, public_key, created_at
           FROM pubkeys WHERE owner_peer_id = ? AND recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_shared_id, recorded_by)
    ))
