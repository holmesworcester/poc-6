"""O(1) sender key distribution via symmetric broadcast.

This event type enables O(1) sender key distribution when all active peers
have converged to the same root secret. Instead of encrypting to O(log n)
copath pubkeys, the sender key is wrapped with the symmetric root secret
that all tree members can decrypt.

Pattern:
- Sender has a sender key (secret) to distribute
- Winning TreeKEM tree has a known root secret
- Wrap sender key with root secret → single event
- All tree members can decrypt with their derived root secret

This is the final optimization for TreeKEM key distribution:
- O(1) for converged tree (this event)
- O(log n) for represented peers (tree cover)
- O(# inactive) for non-represented peers (leaf fallback)

Naming:
- secret_shared: point-to-point, asymmetric encryption to one recipient's pubkey
- secret_broadcast: broadcast, symmetric encryption to shared root secret
"""

# Registry metadata
EVENT_TYPE = 'secret_broadcast'
SHAREABLE = True  # Syncs to peers for decryption
PROJECTION_TABLE = ('secrets_broadcast', 'secret_broadcast_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp, EmitEvent

log = logging.getLogger(__name__)


# Event specification - signed, encrypted with symmetric key
EVENT_SPEC = {
    'encrypted': True,  # Wrapped with root secret (symmetric)
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for secret_broadcast events.

    On success:
    - Verifies the sender key can be reconstructed
    - Inserts into secrets_broadcast table
    - Emits deterministic secret event with the sender key material
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'secret_broadcast':
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Validate required fields
    secret_id = event_data.get('secret_id')
    symmetric_key_b64 = event_data.get('symmetric_key')
    source_update_id = event_data.get('source_update_id')
    created_at = event_data.get('created_at')

    if not secret_id or not symmetric_key_b64 or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Validate symmetric key
    try:
        symmetric_key = crypto.b64decode(symmetric_key_b64)
        if len(symmetric_key) != 32:
            return ProjectorResult(writes=tuple(), valid_event=False)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Verify computed secret_id matches claimed (security check)
    # The secret event is deterministic: hash(encode_secret_wire_event(key, created_at=0))
    deterministic_blob = wire_format.encode_secret_wire_event(
        key=symmetric_key,
        created_at_ms=0,  # Deterministic
    )
    computed_secret_id = crypto.b64encode(crypto.hash(deterministic_blob))

    if computed_secret_id != secret_id:
        log.error(f"secret_broadcast secret_id mismatch: "
                  f"computed={computed_secret_id[:20]}... claimed={secret_id[:20]}...")
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Create secret event data to emit
    secret_event_data = {
        'type': 'secret',
        'key': symmetric_key_b64,
    }

    # Emit deterministic secret event
    emit_secret = EmitEvent(
        event_type='secret',
        event_data=secret_event_data,
        peer_id=None,  # Use recorded_by from context
    )

    # Insert into secrets_broadcast tracking table
    writes = (
        WriteOp(
            op='insert',
            table='secrets_broadcast',
            values={
                'secret_broadcast_id': ctx.event_id,
                'secret_id': computed_secret_id,
                'source_update_id': source_update_id,
                'created_at': created_at,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    log.info(f"secret_broadcast projected for secret={secret_id[:20]}...")

    return ProjectorResult(
        writes=writes,
        valid_event=True,
        emit_events=(emit_secret,),
    )


def create(
    secret_id: str,
    source_update_id: str | None,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create a secret_broadcast event to distribute a sender key via root secret.

    This is the O(1) optimization for sender key distribution. Instead of
    encrypting to O(log n) copath pubkeys, wrap with the symmetric root secret.

    Args:
        secret_id: The sender key ID to distribute
        source_update_id: The treekem_update whose root secret to use (optional)
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (for signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        secret_broadcast_id: The stored event ID

    Raises:
        ValueError: If secret or root secret not found
    """
    log.info(f"secret_broadcast.create() secret={secret_id[:20]}...")

    # Get the sender key material
    from events.group import secret
    secret_record = secret.get_key(secret_id, peer_id, db)
    if not secret_record:
        raise ValueError(f"secret not found: {secret_id}")

    key_bytes = secret_record['key']

    # Get root secret for wrapping
    from events.group import treekem_secret
    root_key = treekem_secret.get_root_secret_key_data(peer_id, db)
    if not root_key:
        raise ValueError("No root secret available - tree has not converged")

    # Get signing key
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wrapped event
    blob = wire_format.encode_secret_broadcast_wire_event(
        secret_id_b64=secret_id,
        symmetric_key_b64=crypto.b64encode(key_bytes),
        source_update_id_b64=source_update_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        root_key=root_key,
        private_key=private_key,
    )

    # Store event
    secret_broadcast_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"secret_broadcast.create() created id={secret_broadcast_id[:20]}...")
    return secret_broadcast_id


def get_broadcast(secret_broadcast_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get a secret_broadcast record by ID.

    Args:
        secret_broadcast_id: The event ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        Broadcast record dict or None if not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return safedb.query_one(
        """SELECT secret_broadcast_id, secret_id, source_update_id,
                  created_at, recorded_at
           FROM secrets_broadcast
           WHERE secret_broadcast_id = ? AND recorded_by = ?""",
        (secret_broadcast_id, recorded_by)
    )


def list_broadcasts(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all secret_broadcast events for a peer.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of broadcast records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT secret_broadcast_id, secret_id, source_update_id,
                  created_at, recorded_at
           FROM secrets_broadcast WHERE recorded_by = ?
           ORDER BY recorded_at DESC""",
        (peer_id,)
    ))
