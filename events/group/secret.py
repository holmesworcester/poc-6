"""TreeKEM secret event type (local-only symmetric key for content encryption).

This replaces group_key for the TreeKEM O(log n) messaging system.
Secrets are local-only, deterministic events that store symmetric key material.
"""

# Registry metadata
EVENT_TYPE = 'secret'
SHAREABLE = False  # Local-only - contains symmetric key material
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Event specification - no signer, local-only deterministic event
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for secret events.

    Secrets are local-only deterministic events (no timestamp in blob).
    Uses recorded_at as created_at.
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'secret':
        return ProjectorResult(writes=tuple(), valid_event=False)

    key_b64 = event_data.get('key')
    if not key_b64:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_secret(key_b64)

    try:
        key_bytes = crypto.b64decode(key_b64)
        if len(key_bytes) != 32:
            return ProjectorResult(writes=tuple(), valid_event=False)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='secrets',
            values={
                'secret_id': ctx.event_id,
                'key': key_bytes,
                'created_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_secret(key_b64: str) -> None:
    """Validate secret fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_secret_plaintext(key=crypto.b64decode(key_b64))
    decoded = wire_format.decode_secret_plaintext(plaintext)
    if decoded["key"] != crypto.b64decode(key_b64):
        raise ValueError("wire shadow decode key mismatch")


def create(peer_id: str, t_ms: int, db: Any) -> str:
    """Create a new secret with a fresh symmetric key.

    Args:
        peer_id: Local peer ID (owner)
        t_ms: Timestamp
        db: Database connection

    Returns:
        secret_id: The event ID
    """
    log.info(f"secret.create() creating new secret for peer_id={peer_id}, t_ms={t_ms}")

    # Generate symmetric key
    key = crypto.generate_secret()

    # Create DETERMINISTIC event blob - only type and key
    _wire_shadow_secret(crypto.b64encode(key))

    blob = wire_format.encode_secret_wire_event(
        key=key,
        created_at_ms=0,  # Deterministic
    )

    # Store event with recorded wrapper and projection
    secret_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"secret.create() created secret_id={secret_id}")
    return secret_id


def create_with_material(key_material: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create secret event with provided key material.

    Creates a DETERMINISTIC secret event from the key material.
    Same key_material = same secret_id on all peers.

    Args:
        key_material: The symmetric key bytes (32 bytes)
        peer_id: Peer ID that owns this key
        t_ms: Timestamp (used for recorded_at, NOT in the blob)
        db: Database connection

    Returns:
        secret_id: The deterministic event ID
    """
    log.info(f"secret.create_with_material() creating secret for peer_id={peer_id}, t_ms={t_ms}")

    # Create DETERMINISTIC event blob
    _wire_shadow_secret(crypto.b64encode(key_material))

    blob = wire_format.encode_secret_wire_event(
        key=key_material,
        created_at_ms=0,  # Deterministic
    )

    secret_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"secret.create_with_material() created secret_id={secret_id}")
    return secret_id


def get_key(secret_id: str, recorded_by: str, db: Any) -> dict[str, Any]:
    """Get secret from database in format expected by crypto.wrap().

    Args:
        secret_id: Base64-encoded secret ID (event ID)
        recorded_by: Peer ID requesting access
        db: Database connection

    Returns:
        Key dict for crypto operations

    Raises:
        ValueError: If secret not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT key FROM secrets WHERE secret_id = ? AND recorded_by = ?",
        (secret_id, recorded_by)
    )
    if not row:
        raise ValueError(f"secret not found: {secret_id}")

    return {
        'id': crypto.b64decode(secret_id),
        'key': row['key'],
        'type': 'symmetric'
    }


def get_key_bytes(secret_id: str, recorded_by: str, db: Any) -> bytes | None:
    """Get raw key bytes for a secret.

    Args:
        secret_id: The secret event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Key bytes or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT key FROM secrets WHERE secret_id = ? AND recorded_by = ?",
        (secret_id, recorded_by)
    )
    return row['key'] if row else None


def list_secrets(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all secrets for a peer.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of secret records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT secret_id, created_at
           FROM secrets WHERE recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id,)
    ))
