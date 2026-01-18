"""Connection prekey event type (device-wide prekey keypair for receiving connection requests).

Renamed from transit_prekey for naming consistency with connection_request/connection_ack.
"""

# Registry metadata
EVENT_TYPE = 'connection_prekey'
SHAREABLE = False  # Local-only - contains private key material
PROJECTION_TABLE = None

from typing import Any
import json
import logging
from core import crypto
from core import store
from core.db import create_unsafe_db, create_safe_db
from core.projection_v2.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)

# Transit prekeys expire after 30 days (in milliseconds)
TRANSIT_PREKEY_TTL_MS = 30 * 24 * 60 * 60 * 1000


# v2 event specification - no signer, no deps (local-only unsigned)
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for connection_prekey events."""
    event_data = ctx.event_data

    public_key_b64 = event_data.get('public_key')
    private_key_b64 = event_data.get('private_key')
    owner_peer_id = event_data.get('signed_by')  # signed_by is the owner
    created_at = event_data.get('created_at')

    if not all([public_key_b64, private_key_b64, owner_peer_id, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Decode keys from base64 to bytes
    public_key = crypto.b64decode(public_key_b64)
    private_key = crypto.b64decode(private_key_b64)

    # Calculate TTL
    ttl_ms = created_at + TRANSIT_PREKEY_TTL_MS

    writes = (
        WriteOp(
            op='insert',
            table='connection_prekeys',
            values={
                'connection_prekey_id': ctx.event_id,
                'owner_peer_id': owner_peer_id,
                'public_key': public_key,
                'private_key': private_key,
                'created_at': created_at,
                'ttl_ms': ttl_ms,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


# Prekey replenishment configuration
MIN_TRANSIT_PREKEYS = 10  # Minimum number of non-expired prekeys to maintain
REPLENISH_TRANSIT_PREKEYS = 20  # Number to generate when below minimum


def generate_batch(peer_id: str, count: int, t_ms: int, db: Any) -> list[str]:
    """Generate N transit prekeys at once.

    Convenience function that calls create() N times with incremented timestamps.

    Args:
        peer_id: Local peer ID creating the prekeys
        count: Number of prekeys to generate
        t_ms: Base timestamp (will be incremented for each prekey)
        db: Database connection (caller handles commit)

    Returns:
        List of connection_prekey_id's created

    Example:
        >>> prekey_ids = generate_batch(alice_peer_id, count=5, t_ms=1000, db=db)
        >>> db.commit()
        >>> assert len(prekey_ids) == 5
    """
    log.info(f"connection_prekey.generate_batch() peer_id={peer_id[:20]}..., count={count}, t_ms={t_ms}")

    prekey_ids = []
    for i in range(count):
        timestamp = t_ms + i
        prekey_id, _ = create(peer_id, timestamp, db)
        prekey_ids.append(prekey_id)

    log.info(f"connection_prekey.generate_batch() generated {len(prekey_ids)} prekeys")
    return prekey_ids


def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a device-wide transit prekey event.

    Generates Ed25519 keypair, stores both public and private keys in event.
    Projects to connection_prekeys table with owner_peer_id.

    Args:
        peer_id: Local peer ID (owner of this prekey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (prekey_id, prekey_private): The stored prekey event ID and private key bytes
    """
    log.info(f"connection_prekey.create() creating new prekey for peer_id={peer_id}, t_ms={t_ms}")

    # Generate Ed25519 keypair for prekey
    prekey_private, prekey_public = crypto.generate_keypair()

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'connection_prekey',
        'public_key': crypto.b64encode(prekey_public),
        'private_key': crypto.b64encode(prekey_private),
        'signed_by': peer_id,  # Local peer who created this prekey
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    unsafedb = create_unsafe_db(db)

    # Store the blob to get prekey_id
    prekey_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    log.info(f"connection_prekey.create() generated prekey_id={prekey_id}")

    # Calculate TTL: absolute time when this prekey expires
    ttl_ms = t_ms + TRANSIT_PREKEY_TTL_MS

    # Insert into connection_prekeys table with TTL
    unsafedb.execute(
        "INSERT OR IGNORE INTO connection_prekeys (connection_prekey_id, owner_peer_id, public_key, private_key, created_at, ttl_ms) VALUES (?, ?, ?, ?, ?, ?)",
        (prekey_id, peer_id, prekey_public, prekey_private, t_ms, ttl_ms)
    )

    # Create recorded wrapper where peer sees itself
    from events.network import recorded
    recorded_id = recorded.create(prekey_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    log.info(f"connection_prekey.create() projected prekey_id={prekey_id}, ttl_ms={ttl_ms}")
    return prekey_id, prekey_private


def create_with_material(public_key: bytes, private_key: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create a transit prekey event with provided key material (for invite prekeys).

    Args:
        public_key: Ed25519 public key bytes
        private_key: Ed25519 private key bytes
        peer_id: Local peer ID (owner of this prekey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        prekey_id: The stored prekey event ID
    """
    log.info(f"connection_prekey.create_with_material() creating prekey for peer_id={peer_id}, t_ms={t_ms}")

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'connection_prekey',
        'public_key': crypto.b64encode(public_key),
        'private_key': crypto.b64encode(private_key),
        'signed_by': peer_id,  # Local peer who created this prekey
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    unsafedb = create_unsafe_db(db)

    # Store the blob to get prekey_id
    prekey_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    log.info(f"connection_prekey.create_with_material() generated prekey_id={prekey_id}")

    # Calculate TTL: absolute time when this prekey expires
    ttl_ms = t_ms + TRANSIT_PREKEY_TTL_MS

    # Insert into connection_prekeys table with TTL
    unsafedb.execute(
        "INSERT OR IGNORE INTO connection_prekeys (connection_prekey_id, owner_peer_id, public_key, private_key, created_at, ttl_ms) VALUES (?, ?, ?, ?, ?, ?)",
        (prekey_id, peer_id, public_key, private_key, t_ms, ttl_ms)
    )

    # Create recorded wrapper where peer sees itself
    from events.network import recorded
    recorded_id = recorded.create(prekey_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    log.info(f"connection_prekey.create_with_material() projected prekey_id={prekey_id}, ttl_ms={ttl_ms}")
    return prekey_id


def project(prekey_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project transit prekey event into connection_prekeys table with owner_peer_id.

    Returns:
        prekey_id on success, None on failure
    """
    log.info(f"connection_prekey.project() prekey_id={prekey_id[:30]}..., seen_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)

    # Get blob from store
    blob = store.get(prekey_id, unsafedb)
    if not blob:
        log.warning(f"connection_prekey.project() blob not found for prekey_id={prekey_id}")
        return None

    # Parse JSON
    event_data = crypto.parse_json(blob)
    owner_peer_id = event_data['signed_by']
    created_at = event_data['created_at']

    # Calculate TTL: absolute time when this prekey expires
    ttl_ms = created_at + TRANSIT_PREKEY_TTL_MS

    # Insert into connection_prekeys table with owner (device-wide)
    unsafedb.execute(
        "INSERT OR IGNORE INTO connection_prekeys (connection_prekey_id, owner_peer_id, public_key, private_key, created_at, ttl_ms) VALUES (?, ?, ?, ?, ?, ?)",
        (prekey_id, owner_peer_id, crypto.b64decode(event_data['public_key']),
         crypto.b64decode(event_data['private_key']), created_at, ttl_ms)
    )

    return prekey_id


def get_connection_prekey_for_peer(peer_shared_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the transit pre-key for a specific peer in format expected by crypto.wrap().

    Prekeys are public and meant to be shared for encryption, indexed by peer_shared_id
    (public identity) rather than local peer_id.

    Args:
        peer_shared_id: Peer's peer_shared_id (public identity) to get prekey for
        recorded_by: Local peer_id requesting access (for subjective view)
        db: Database connection

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'type': 'asymmetric'}
        or None if prekey not found
    """
    import logging
    lookup_log = logging.getLogger(__name__)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    lookup_log.info(f"get_connection_prekey_for_peer() looking for prekey of peer_shared_id={peer_shared_id[:20]}..., requested_by={recorded_by[:20]}...")

    # Debug: show all connection_prekeys_shared for this recorded_by
    all_shared = safedb.query("SELECT peer_id, connection_prekey_id, connection_prekey_shared_id FROM connection_prekeys_shared WHERE recorded_by = ?", (recorded_by,))
    lookup_log.info(f"get_connection_prekey_for_peer() ALL connection_prekeys_shared for {recorded_by[:20]}: {[(r['peer_id'][:20], r['connection_prekey_id'][:20] if r['connection_prekey_id'] else 'NULL', r['connection_prekey_shared_id'][:20]) for r in all_shared[:5]]}")

    result = safedb.query_one(
        "SELECT connection_prekey_shared_id, connection_prekey_id, public_key FROM connection_prekeys_shared WHERE peer_id = ? AND recorded_by = ? ORDER BY created_at DESC, connection_prekey_shared_id DESC LIMIT 1",
        (peer_shared_id, recorded_by)
    )

    if not result:
        # Fallback: check invite_accepteds for bootstrap case (inviter's transit prekey from invite link)
        result = safedb.query_one(
            """SELECT inviter_connection_prekey_id as connection_prekey_id,
                      inviter_connection_prekey_public_key as public_key
               FROM invite_accepteds
               WHERE inviter_peer_shared_id = ? AND recorded_by = ?
               AND inviter_connection_prekey_public_key IS NOT NULL
               LIMIT 1""",
            (peer_shared_id, recorded_by)
        )
        if result:
            lookup_log.info(f"get_connection_prekey_for_peer() FOUND via invite_accepteds for peer_shared_id={peer_shared_id[:20]}...")

    if not result:
        lookup_log.warning(f"get_connection_prekey_for_peer() NO PREKEY FOUND for peer_shared_id={peer_shared_id[:20]}...")
        return None

    # Use connection_prekey_id as the crypto hint (matches prekey_id in recipient's connection_prekeys table)
    connection_prekey_id_bytes = crypto.b64decode(result['connection_prekey_id'])
    lookup_log.info(f"get_connection_prekey_for_peer() FOUND connection_prekey_id={result['connection_prekey_id'][:20]}... for peer_shared_id={peer_shared_id[:20]}...")

    return {
        'id': connection_prekey_id_bytes,
        'public_key': result['public_key'],
        'type': 'asymmetric'
    }


def replenish_for_all_peers(t_ms: int, db: Any) -> dict[str, Any]:
    """Replenish transit prekeys for all local peers if running low.

    This is a recurring job that should run periodically (every 1-6 hours)
    to ensure each peer has enough non-expired prekeys available.

    For each local peer:
    1. Count non-expired transit prekeys
    2. If count < MIN_TRANSIT_PREKEYS, generate REPLENISH_TRANSIT_PREKEYS new ones

    Args:
        t_ms: Current time in milliseconds
        db: Database connection

    Returns:
        Dict with stats: {
            'peers_processed': int,
            'peers_replenished': int,
            'total_prekeys_generated': int,
            'errors': list[str]
        }

    Note: Each peer's prekeys are checked independently. If a peer has enough
    prekeys, no action is taken. Errors for one peer do not stop processing
    of other peers.
    """
    log.info(f"connection_prekey.replenish_for_all_peers() t_ms={t_ms}")

    unsafedb = create_unsafe_db(db)

    stats = {
        'peers_processed': 0,
        'peers_replenished': 0,
        'total_prekeys_generated': 0,
        'errors': []
    }

    # Get all local peers
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")

    if not local_peer_rows:
        log.info(f"connection_prekey.replenish_for_all_peers() no local peers found")
        return stats

    log.info(f"connection_prekey.replenish_for_all_peers() found {len(local_peer_rows)} local peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']
        try:
            # Count non-expired prekeys for this peer
            prekey_count_row = unsafedb.query_one(
                """SELECT COUNT(*) as count FROM connection_prekeys
                   WHERE owner_peer_id = ? AND ttl_ms > ?""",
                (peer_id, t_ms)
            )
            prekey_count = prekey_count_row['count'] if prekey_count_row else 0

            log.debug(f"connection_prekey.replenish_for_all_peers() peer {peer_id[:20]}... has {prekey_count} non-expired prekeys")

            stats['peers_processed'] += 1

            # Replenish if below minimum
            if prekey_count < MIN_TRANSIT_PREKEYS:
                log.info(f"connection_prekey.replenish_for_all_peers() peer {peer_id[:20]}... has only {prekey_count} prekeys, replenishing with {REPLENISH_TRANSIT_PREKEYS}")

                prekey_ids = generate_batch(peer_id, REPLENISH_TRANSIT_PREKEYS, t_ms, db)

                # Project each prekey
                for i, prekey_id in enumerate(prekey_ids):
                    project(prekey_id, peer_id, t_ms + i, db)

                stats['peers_replenished'] += 1
                stats['total_prekeys_generated'] += len(prekey_ids)
                log.info(f"connection_prekey.replenish_for_all_peers() generated {len(prekey_ids)} prekeys for peer {peer_id[:20]}...")

        except Exception as e:
            error = f"Error processing peer {peer_id[:20]}...: {e}"
            log.error(f"connection_prekey.replenish_for_all_peers() {error}")
            stats['errors'].append(error)
            continue

    log.info(f"connection_prekey.replenish_for_all_peers() complete: {stats['peers_processed']} peers processed, {stats['peers_replenished']} replenished, {stats['total_prekeys_generated']} prekeys generated")
    return stats
