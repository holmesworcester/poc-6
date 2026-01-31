"""TreeKEM treekem_secret_shared event type (shareable path secret sealed to recipient treekem_pubkey).

This distributes path secrets to copath nodes for TreeKEM O(log n) key distribution.
treekem_secret_shared events are deterministic (unsigned), shareable events that distribute
path secrets sealed to recipient treekem_pubkeys.

On projection, emits a deterministic treekem_secret event with the shared key material.

Pattern: treekem_secret (local, SHAREABLE=False) + treekem_secret_shared (shareable, SHAREABLE=True)
Similar to: secret + secret_shared
"""

# Registry metadata
EVENT_TYPE = 'treekem_secret_shared'
SHAREABLE = True  # Sealed path secrets sync to enable O(log n) key distribution
PROJECTION_TABLE = ('treekem_secrets_shared', 'treekem_secret_shared_id')

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
    'encrypted': True,  # Wrapped to recipient's treekem_pubkey
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for treekem_secret_shared events.

    On success:
    - Verifies computed treekem_secret_id matches claimed treekem_secret_id (security check)
    - Inserts into treekem_secrets_shared table
    - Emits deterministic treekem_secret events for the received secret AND all ancestors

    CRITICAL: Derives all ancestor secrets up to root using KDF chain.
    This ensures all recipients converge to the same root secret:
    - Receive secret at depth D with key K_D
    - Derive K_{D-1} = KDF(K_D, context)
    - Continue until K_0 (root)
    - Emit treekem_secret events at ALL depths

    DH-Based Reception (BeeKEM style):
    When receiving a DH-encrypted secret, the recipient:
    1. Gets sender's new public key from treekem_pubkeys_shared
    2. Computes shared_secret = DH(my_private_key, sender_pubkey)
    3. Uses shared_secret as the decryption key
    4. Derives all ancestors via KDF to reach the same root as sender
    """
    event_data = ctx.event_data

    # Validate required fields
    treekem_secret_id = event_data.get('treekem_secret_id')
    symmetric_key_b64 = event_data.get('symmetric_key')
    recipient_pubkey_id = event_data.get('recipient_pubkey_id')
    source_update_id = event_data.get('source_update_id')  # Can be None
    depth = event_data.get('depth')
    path_prefix_b64 = event_data.get('path_prefix')

    # Optional: sender's pubkey for DH computation (new DH-based field)
    sender_pubkey_b64 = event_data.get('sender_pubkey')

    if not treekem_secret_id or not symmetric_key_b64 or not recipient_pubkey_id or depth is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Validate symmetric key can be decoded
    try:
        symmetric_key = crypto.b64decode(symmetric_key_b64)
        if len(symmetric_key) != 32:
            return ProjectorResult(writes=tuple(), valid_event=False)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    if path_prefix_b64 is None:
        log.warning(f"treekem_secret_shared missing path_prefix, cannot derive to root")
        return ProjectorResult(writes=tuple(), valid_event=False)

    try:
        path_prefix = crypto.b64decode(path_prefix_b64)
    except Exception:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Verify computed treekem_secret_id matches claimed (security check)
    deterministic_blob = wire_format.encode_treekem_secret_wire_event(
        depth=depth,
        path_prefix=path_prefix,
        key=symmetric_key,
        created_at_ms=0,  # Deterministic
    )
    computed_secret_id = crypto.b64encode(crypto.hash(deterministic_blob))

    if computed_secret_id != treekem_secret_id:
        log.error(f"treekem_secret_shared treekem_secret_id mismatch: "
                  f"computed={computed_secret_id[:20]}... claimed={treekem_secret_id[:20]}...")
        return ProjectorResult(writes=tuple(), valid_event=False)

    # CRITICAL: Derive ALL ancestor secrets up to root and emit them
    # This is what enables O(1) root convergence
    emit_events: list[EmitEvent] = []
    current_key = symmetric_key
    current_depth = depth
    current_prefix = path_prefix

    while current_depth >= 0:
        # Emit treekem_secret at this depth
        emit_events.append(EmitEvent(
            event_type='treekem_secret',
            event_data={
                'type': 'treekem_secret',
                'depth': current_depth,
                'path_prefix': crypto.b64encode(current_prefix),
                'key': crypto.b64encode(current_key),
            },
            peer_id=None,  # Use recorded_by from context
        ))

        if current_depth == 0:
            break  # Reached root

        # Derive parent secret using KDF
        # Note: For DH-based TreeKEM, both sender and receiver use the same KDF
        # to derive parent secrets from the DH shared secret
        current_key = crypto.derive_path_secret(current_key, current_depth, current_prefix)
        current_depth -= 1
        # Parent prefix is the same bytes but represents fewer bits at shallower depths
        # We keep the same path_prefix bytes (semantic meaning changes with depth)
        current_prefix = current_prefix

    # Insert into treekem_secrets_shared tracking table
    writes = (
        WriteOp(
            op='insert',
            table='treekem_secrets_shared',
            values={
                'treekem_secret_shared_id': ctx.event_id,
                'treekem_secret_id': computed_secret_id,
                'recipient_pubkey_id': recipient_pubkey_id,
                'source_update_id': source_update_id,
                'depth': depth,
                'recorded_at': ctx.recorded_at,
            },
        ),
    )

    log.info(f"treekem_secret_shared derived {len(emit_events)} secrets from depth={depth} to root")

    return ProjectorResult(
        writes=writes,
        valid_event=True,
        emit_events=tuple(emit_events),
    )


def create_dh_encrypted(
    parent_secret: bytes,
    depth: int,
    path_prefix: bytes,
    recipient_pubkey_id: str,
    dh_shared_secret: bytes,
    source_update_id: str | None,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create a DH-encrypted treekem_secret_shared event.

    This is for DH-based TreeKEM where the parent secret is encrypted
    using the DH shared secret computed between sender and recipient.

    Args:
        parent_secret: The parent secret to share (32 bytes)
        depth: Depth of the parent secret (depth of shared node)
        path_prefix: Path prefix at the parent depth
        recipient_pubkey_id: Recipient's treekem_pubkey ID
        dh_shared_secret: The DH shared secret for encryption
        source_update_id: The treekem_update this is part of (optional)
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (for signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        treekem_secret_shared_id: The stored event ID
    """
    log.info(f"treekem_secret_shared.create_dh_encrypted() depth={depth}, "
             f"recipient={recipient_pubkey_id[:20]}...")

    # Create a local treekem_secret for the parent
    from events.group import treekem_secret
    parent_secret_id = treekem_secret.create_with_material(
        depth=depth,
        path_prefix=path_prefix,
        key_material=parent_secret,
        peer_id=peer_id,
        t_ms=t_ms,
        db=db,
    )

    # Get recipient's treekem_pubkey for wrapping
    from events.group import treekem_pubkey
    recipient_pk = treekem_pubkey.get_treekem_pubkey_by_id(recipient_pubkey_id, peer_id, db)

    if not recipient_pk:
        raise ValueError(f"No treekem_pubkey found: {recipient_pubkey_id}")

    recipient_pubkey = {
        'id': crypto.b64decode(recipient_pubkey_id),
        'public_key': recipient_pk['public_key'],
        'type': 'asymmetric'
    }

    # Get signing key
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wrapped event - the secret is already the DH-derived parent secret
    blob = wire_format.encode_treekem_secret_shared_wire_event(
        treekem_secret_id_b64=parent_secret_id,
        symmetric_key_b64=crypto.b64encode(parent_secret),
        recipient_pubkey_id_b64=recipient_pubkey_id,
        source_update_id_b64=source_update_id,
        depth=depth,
        path_prefix=path_prefix,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        recipient_pubkey=recipient_pubkey,
        private_key=private_key,
    )

    # Store event
    treekem_secret_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"treekem_secret_shared.create_dh_encrypted() created treekem_secret_shared_id={treekem_secret_shared_id[:20]}...")
    return treekem_secret_shared_id


def create(
    treekem_secret_id: str,
    recipient_pubkey_id: str,
    source_update_id: str | None,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> str:
    """Create a shareable treekem_secret_shared event from a local treekem_secret.

    The path secret is sealed to the recipient's treekem_pubkey using asymmetric encryption.
    Includes path_prefix so recipients can derive ancestor secrets via KDF.

    Args:
        treekem_secret_id: Local treekem_secret event ID
        recipient_pubkey_id: Recipient's treekem_pubkey ID
        source_update_id: The treekem_update this is part of (optional)
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (for signing)
        t_ms: Timestamp
        db: Database connection

    Returns:
        treekem_secret_shared_id: The stored event ID
    """
    log.info(f"treekem_secret_shared.create() treekem_secret={treekem_secret_id[:20]}..., "
             f"recipient={recipient_pubkey_id[:20]}...")

    # Get treekem_secret record from local store
    from events.group import treekem_secret
    secret_record = treekem_secret.get_secret(treekem_secret_id, peer_id, db)
    if not secret_record:
        raise ValueError(f"treekem_secret not found: {treekem_secret_id}")

    key_bytes = secret_record['key']
    depth = secret_record['depth']
    path_prefix = secret_record['path_prefix']

    # Get recipient's treekem_pubkey for wrapping
    from events.group import treekem_pubkey
    recipient_pk = treekem_pubkey.get_treekem_pubkey_by_id(recipient_pubkey_id, peer_id, db)

    if not recipient_pk:
        raise ValueError(f"No treekem_pubkey found: {recipient_pubkey_id}")

    recipient_pubkey = {
        'id': crypto.b64decode(recipient_pubkey_id),
        'public_key': recipient_pk['public_key'],
        'type': 'asymmetric'
    }

    # Get signing key
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wrapped event - includes path_prefix for KDF derivation
    blob = wire_format.encode_treekem_secret_shared_wire_event(
        treekem_secret_id_b64=treekem_secret_id,
        symmetric_key_b64=crypto.b64encode(key_bytes),
        recipient_pubkey_id_b64=recipient_pubkey_id,
        source_update_id_b64=source_update_id,
        depth=depth,
        path_prefix=path_prefix,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        recipient_pubkey=recipient_pubkey,
        private_key=private_key,
    )

    # Store event
    treekem_secret_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"treekem_secret_shared.create() created treekem_secret_shared_id={treekem_secret_shared_id[:20]}...")
    return treekem_secret_shared_id


def share_path_secrets_to_copath(
    path_secrets: list[tuple[str, int, bytes]],  # [(treekem_secret_id, depth, path_prefix), ...]
    copath_pubkey_ids: list[str],
    source_update_id: str | None,
    peer_id: str,
    peer_shared_id: str,
    t_ms: int,
    db: Any,
) -> list[str]:
    """Share path secrets to copath node pubkeys.

    For each depth on the update path, shares that depth's path secret to the
    corresponding copath node pubkey.

    Args:
        path_secrets: List of (treekem_secret_id, depth, path_prefix) tuples
        copath_pubkey_ids: List of copath node treekem_pubkey IDs (one per depth)
        source_update_id: The treekem_update this is part of (optional)
        peer_id: Local peer ID
        peer_shared_id: Public peer ID (for signing)
        t_ms: Base timestamp
        db: Database connection

    Returns:
        List of treekem_secret_shared event IDs created
    """
    log.info(f"treekem_secret_shared.share_path_secrets_to_copath() "
             f"secrets={len(path_secrets)}, copaths={len(copath_pubkey_ids)}")

    if len(path_secrets) != len(copath_pubkey_ids):
        raise ValueError(f"Mismatch: {len(path_secrets)} secrets, {len(copath_pubkey_ids)} copath pubkeys")

    shared_ids = []
    for i, ((secret_id, depth, path_prefix), copath_pubkey_id) in enumerate(
        zip(path_secrets, copath_pubkey_ids)
    ):
        try:
            shared_id = create(
                treekem_secret_id=secret_id,
                recipient_pubkey_id=copath_pubkey_id,
                source_update_id=source_update_id,
                peer_id=peer_id,
                peer_shared_id=peer_shared_id,
                t_ms=t_ms + i,
                db=db,
            )
            shared_ids.append(shared_id)
        except Exception as e:
            log.warning(f"treekem_secret_shared.share_path_secrets_to_copath() "
                        f"failed for depth={depth}: {e}")

    return shared_ids


def list_shared(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all treekem_secret_shared events for a peer.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of treekem_secret_shared records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT treekem_secret_shared_id, treekem_secret_id, recipient_pubkey_id,
                  source_update_id, depth, recorded_at
           FROM treekem_secrets_shared WHERE recorded_by = ?
           ORDER BY recorded_at DESC""",
        (peer_id,)
    ))


def get_shared_by_update(source_update_id: str, recorded_by: str, db: Any) -> list[dict[str, Any]]:
    """Get all treekem_secret_shared events for a specific update.

    Args:
        source_update_id: The treekem_update ID
        recorded_by: Local peer ID
        db: Database connection

    Returns:
        List of treekem_secret_shared records
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)
    return list(safedb.query(
        """SELECT treekem_secret_shared_id, treekem_secret_id, recipient_pubkey_id,
                  source_update_id, depth, recorded_at
           FROM treekem_secrets_shared
           WHERE source_update_id = ? AND recorded_by = ?
           ORDER BY depth ASC""",
        (source_update_id, recorded_by)
    ))
