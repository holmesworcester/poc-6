"""TreeKEM secret_shared event type (shareable symmetric key sealed to recipient pubkey).

This replaces group_key_shared for the TreeKEM O(log n) messaging system.
Secret_shared events are deterministic (unsigned), shareable events that distribute
symmetric keys sealed to recipient public keys. They include a removal_epoch_id
to support forward secrecy.
"""

# Registry metadata
EVENT_TYPE = 'secret_shared'
SHAREABLE = True  # Sealed keys sync to enable group decryption
PROJECTION_TABLE = ('secrets_shared', 'secret_shared_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp, EmitEvent

log = logging.getLogger(__name__)


# Event specification - deterministic (unsigned), encrypted
EVENT_SPEC = {
    'encrypted': True,  # Wrapped to recipient's pubkey
    'signer': None,  # Deterministic - no signature
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for secret_shared events.

    On success:
    - Verifies computed secret_id matches claimed secret_id (security check)
    - Inserts into secrets_shared table
    - Emits deterministic secret event with the shared key material
    """
    event_data = ctx.event_data

    # Validate required fields
    secret_id = event_data.get('secret_id')
    symmetric_key_b64 = event_data.get('symmetric_key')
    recipient_pubkey_id = event_data.get('recipient_pubkey_id')
    removal_epoch_id = event_data.get('removal_epoch_id')  # Can be None for initial keys

    if not secret_id or not symmetric_key_b64 or not recipient_pubkey_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Validate symmetric key can be decoded
    try:
        symmetric_key = crypto.b64decode(symmetric_key_b64)
        if len(symmetric_key) != 32:
            return ProjectorResult(writes=tuple(), valid_event=False)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Create deterministic secret event data
    secret_event_data = {
        'type': 'secret',
        'key': symmetric_key_b64,
    }

    # Verify the computed secret_id matches the claimed secret_id
    # Security check: prevents sender from claiming wrong secret_id
    deterministic_blob = wire_format.encode_secret_wire_event(
        key=symmetric_key,
        created_at_ms=0,  # Deterministic
    )
    computed_secret_id = crypto.b64encode(crypto.hash(deterministic_blob))
    if computed_secret_id != secret_id:
        log.error(f"secret_shared secret_id mismatch: computed={computed_secret_id[:20]}... claimed={secret_id[:20]}...")
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Emit deterministic secret event
    emit_secret = EmitEvent(
        event_type='secret',
        event_data=secret_event_data,
        peer_id=None,  # Use recorded_by from context
    )

    # Insert into secrets_shared tracking table
    writes = (
        WriteOp(
            op='insert',
            table='secrets_shared',
            values={
                'secret_shared_id': ctx.event_id,
                'original_secret_id': computed_secret_id,
                'recipient_pubkey_id': recipient_pubkey_id,
                'removal_epoch_id': removal_epoch_id,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    return ProjectorResult(
        writes=writes,
        valid_event=True,
        emit_events=(emit_secret,),
    )


def create(secret_id: str, peer_id: str, peer_shared_id: str, recipient_peer_id: str,
           removal_epoch_id: str | None, t_ms: int, db: Any) -> str:
    """Create a shareable secret_shared event from a local secret.

    The secret is sealed to the recipient's pubkey using asymmetric encryption.

    Args:
        secret_id: Local secret event ID
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (for signing)
        recipient_peer_id: Recipient's peer_shared_id
        removal_epoch_id: Current removal epoch (for forward secrecy) or None
        t_ms: Timestamp
        db: Database connection

    Returns:
        secret_shared_id: The stored event ID
    """
    log.info(f"secret_shared.create() secret_id={secret_id}, recipient={recipient_peer_id}, t_ms={t_ms}")

    # Get secret key material from local store
    from events.group import secret
    key_bytes = secret.get_key_bytes(secret_id, peer_id, db)
    if not key_bytes:
        raise ValueError(f"secret not found: {secret_id}")

    # Get recipient's pubkey for wrapping
    from events.group import pubkey
    recipient_pubkey = pubkey.get_pubkey_for_peer(recipient_peer_id, peer_id, db)
    if not recipient_pubkey:
        raise ValueError(f"No pubkey found for recipient peer: {recipient_peer_id}")

    recipient_pubkey_id = crypto.b64encode(recipient_pubkey['id'])

    # Get signing key
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wrapped event
    blob = wire_format.encode_secret_shared_wire_event(
        secret_id_b64=secret_id,
        symmetric_key_b64=crypto.b64encode(key_bytes),
        recipient_pubkey_id_b64=recipient_pubkey_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        recipient_pubkey=recipient_pubkey,
        private_key=private_key,
    )

    # Store event
    secret_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"secret_shared.create() created secret_shared_id={secret_shared_id}")
    return secret_shared_id


def create_to_pubkey(
    secret_id: str,
    peer_id: str,
    peer_shared_id: str,
    recipient_pubkey_id: str,
    removal_epoch_id: str | None,
    t_ms: int,
    db: Any,
) -> str:
    """Create a secret_shared event sealed to a specific pubkey.

    This supports sharing to any pubkey type (peer pubkey or treekem_pubkey).

    Args:
        secret_id: Local secret event ID
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (for signing)
        recipient_pubkey_id: Recipient's pubkey ID (can be pubkey or treekem_pubkey)
        removal_epoch_id: Current removal epoch (for forward secrecy) or None
        t_ms: Timestamp
        db: Database connection

    Returns:
        secret_shared_id: The stored event ID
    """
    log.info(f"secret_shared.create_to_pubkey() secret_id={secret_id[:20]}..., recipient_pubkey={recipient_pubkey_id[:20]}...")

    # Get secret key material from local store
    from events.group import secret
    key_bytes = secret.get_key_bytes(secret_id, peer_id, db)
    if not key_bytes:
        raise ValueError(f"secret not found: {secret_id}")

    # Try to get recipient pubkey - first try regular pubkey, then treekem_pubkey
    from events.group import pubkey
    recipient_pk = pubkey.get_pubkey_by_id(recipient_pubkey_id, peer_id, db)

    if not recipient_pk:
        # Try treekem_pubkey
        from events.group import treekem_pubkey
        recipient_pk = treekem_pubkey.get_pubkey_by_id(recipient_pubkey_id, peer_id, db)

    if not recipient_pk:
        raise ValueError(f"No pubkey found: {recipient_pubkey_id}")

    recipient_pubkey = {
        'id': crypto.b64decode(recipient_pubkey_id),
        'public_key': recipient_pk['public_key'],
        'type': 'asymmetric'
    }

    # Get signing key
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wrapped event
    blob = wire_format.encode_secret_shared_wire_event(
        secret_id_b64=secret_id,
        symmetric_key_b64=crypto.b64encode(key_bytes),
        recipient_pubkey_id_b64=recipient_pubkey_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        recipient_pubkey=recipient_pubkey,
        private_key=private_key,
    )

    # Store event
    secret_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"secret_shared.create_to_pubkey() created secret_shared_id={secret_shared_id}")
    return secret_shared_id


def share_secret_with_pubkeys(
    secret_id: str,
    peer_id: str,
    peer_shared_id: str,
    recipient_pubkey_ids: list[str],
    removal_epoch_id: str | None,
    t_ms: int,
    db: Any,
) -> list[str]:
    """Create secret_shared events for multiple pubkey recipients.

    This supports sharing to any pubkey type (peer pubkey or treekem_pubkey).

    Args:
        secret_id: The secret to share
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (for signing)
        recipient_pubkey_ids: List of recipient pubkey IDs
        removal_epoch_id: Current removal epoch or None
        t_ms: Base timestamp
        db: Database connection

    Returns:
        List of secret_shared event IDs created
    """
    log.info(f"secret_shared.share_secret_with_pubkeys() secret={secret_id[:20]}..., recipients={len(recipient_pubkey_ids)}")

    shared_ids = []
    for i, recipient_pubkey_id in enumerate(recipient_pubkey_ids):
        try:
            shared_id = create_to_pubkey(
                secret_id=secret_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                recipient_pubkey_id=recipient_pubkey_id,
                removal_epoch_id=removal_epoch_id,
                t_ms=t_ms + i,
                db=db,
            )
            shared_ids.append(shared_id)
        except Exception as e:
            log.warning(f"secret_shared.share_secret_with_pubkeys() failed for {recipient_pubkey_id[:20]}...: {e}")

    return shared_ids


def share_secret_with_peers(secret_id: str, peer_id: str, recipient_peer_ids: list[str],
                             removal_epoch_id: str | None, t_ms: int, db: Any) -> list[str]:
    """Create secret_shared events for multiple recipients.

    Args:
        secret_id: The secret to share
        peer_id: Local peer ID
        recipient_peer_ids: List of recipient peer_shared_ids
        removal_epoch_id: Current removal epoch or None
        t_ms: Base timestamp
        db: Database connection

    Returns:
        List of secret_shared event IDs created
    """
    log.info(f"secret_shared.share_secret_with_peers() secret={secret_id[:20]}..., recipients={len(recipient_peer_ids)}")

    secret_shared_ids = []
    for i, recipient_peer_id in enumerate(recipient_peer_ids):
        try:
            secret_shared_id = create(
                secret_id=secret_id,
                peer_id=peer_id,
                recipient_peer_id=recipient_peer_id,
                removal_epoch_id=removal_epoch_id,
                t_ms=t_ms + i,
                db=db
            )
            secret_shared_ids.append(secret_shared_id)
            log.info(f"secret_shared.share_secret_with_peers() shared to {recipient_peer_id[:20]}...")
        except Exception as e:
            log.warning(f"secret_shared.share_secret_with_peers() failed for {recipient_peer_id[:20]}...: {e}")

    return secret_shared_ids


def get_secrets_for_epoch(removal_epoch_id: str | None, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """Get all secrets shared under a specific removal epoch.

    Args:
        removal_epoch_id: The removal epoch ID (or None for initial secrets)
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of secret_shared records
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    if removal_epoch_id is None:
        return list(safedb.query(
            """SELECT * FROM secrets_shared
               WHERE removal_epoch_id IS NULL AND recorded_by = ?""",
            (recorded_by,)
        ))
    else:
        return list(safedb.query(
            """SELECT * FROM secrets_shared
               WHERE removal_epoch_id = ? AND recorded_by = ?""",
            (removal_epoch_id, recorded_by)
        ))


def list(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all secret_shared events for a peer.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of secret_shared records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT * FROM secrets_shared
           WHERE recorded_by = ?
           ORDER BY recorded_at DESC""",
        (peer_id,)
    ))
