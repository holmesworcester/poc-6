"""Peer event type (local-only identity keypair)."""
from typing import Any

# Registry metadata
EVENT_TYPE = 'peer'
SHAREABLE = False  # Local-only - contains private key material
PROJECTION_TABLE = None  # No projection table (stored in peers table)
import json
import logging
from core import crypto
from core import store
from core.db import create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# v2 event specification - no signer, no deps (local-only unsigned event)
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for peer events.

    Peer events are local-only (unsigned, unencrypted) and write to local_peers.
    """
    event_data = ctx.event_data

    public_key = event_data.get('public_key')
    private_key_b64 = event_data.get('private_key')
    created_at = event_data.get('created_at')

    if not public_key or not private_key_b64 or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Decode private key from base64 to bytes for storage
    private_key = crypto.b64decode(private_key_b64)

    writes = (
        WriteOp(
            op='insert',
            table='local_peers',
            values={
                'peer_id': ctx.event_id,
                'public_key': public_key,  # Keep as base64 string
                'private_key': private_key,  # Store as bytes
                'created_at': created_at,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def create(t_ms: int, db: Any) -> str:
    """Create a peer (local-only keypair).

    Returns peer_id: the local peer ID for signing operations.

    Note: peer_shared is NOT created here. It is created during network join
    (via invite) so that every peer_shared is invite-signed and linked to a user_id.
    """
    log.info(f"peer.create() creating new peer at t_ms={t_ms}")

    unsafedb = create_unsafe_db(db)

    # Generate keypair
    private_key, public_key = crypto.generate_keypair()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'peer',
        'public_key': crypto.b64encode(public_key),
        'private_key': crypto.b64encode(private_key),
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # First store the blob to get the peer_id
    peer_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    log.info(f"peer.create() generated peer_id={peer_id}")

    # Then create recorded wrapper where peer sees itself
    from events.network import recorded
    recorded_id = recorded.create(peer_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    return peer_id


def project(peer_id: str, recorded_by: str, recorded_at: int, db: Any) -> str:
    """Project peer event into peers table (for local peers, both IDs are the same)."""
    log.debug(f"peer.project() projecting peer_id={peer_id}, seen_by={recorded_by}")

    unsafedb = create_unsafe_db(db)
    from core.db import create_safe_db
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(peer_id, unsafedb)
    if not blob:
        log.warning(f"peer.project() blob not found for peer_id={peer_id}")
        return

    # Parse JSON
    event_data = crypto.parse_json(blob)

    # Insert into local_peers table (local-only, not shareable)
    # Note: peer_id → peer_shared_id mapping is stored in peer_self table (subjective)
    unsafedb.execute(
        """INSERT OR IGNORE INTO local_peers (peer_id, public_key, private_key, created_at)
           VALUES (?, ?, ?, ?)""",
        (
            peer_id,
            event_data['public_key'],
            crypto.b64decode(event_data['private_key']),
            event_data['created_at']
        )
    )

    # Mark as valid immediately (before returning) to ensure other events
    # see this peer as valid during dependency checking. This is critical
    # for convergence - the timing of when events become valid matters.
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (peer_id, recorded_by)
    )

    log.info(f"peer.project() projected peer_id={peer_id} into peers table")

    return peer_id


def get_private_key(peer_id: str, recorded_by: str, db: Any) -> bytes:
    """Get private key for a peer_id.

    Args:
        peer_id: Peer ID to get private key for
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Private key bytes

    Raises:
        ValueError: If peer not found or recorded_by != peer_id (can only access your own private key)
    """
    # Security: Only allow a peer to access their own private key
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access private key for peer {peer_id}")

    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT private_key FROM local_peers WHERE peer_id = ?", (peer_id,))
    if not row:
        raise ValueError(f"peer not found: {peer_id}")
    return row['private_key']


def get_public_key(peer_id: str, recorded_by: str, db: Any) -> bytes:
    """Get public key for a peer_id from local_peers table.

    Args:
        peer_id: Peer ID to get public key for
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Public key bytes

    Raises:
        ValueError: If peer not found or recorded_by != peer_id (can only access your own peer's public key from local_peers)
    """
    # Security: Only allow a peer to access their own local peer's public key
    # (Public keys for other peers should come from peer_shared events, not local_peers)
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access local peer public key for peer {peer_id}")

    unsafedb = create_unsafe_db(db)
    row = unsafedb.query_one("SELECT public_key FROM local_peers WHERE peer_id = ?", (peer_id,))
    if not row:
        raise ValueError(f"peer not found: {peer_id}")
    # public_key is stored as base64 string in the table
    return crypto.b64decode(row['public_key'])


def list_local(db: Any) -> list[str]:
    """List all local peer IDs we control.

    Returns:
        List of peer_id strings
    """
    unsafedb = create_unsafe_db(db)
    rows = unsafedb.query("SELECT peer_id FROM local_peers")
    return [row['peer_id'] for row in rows]
