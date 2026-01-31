"""Connection pubkey event type (device-wide keypair for receiving connection requests).

Renamed from connection_prekey for naming consistency (prekey → pubkey).
"""

# Registry metadata
EVENT_TYPE = 'connection_pubkey'
SHAREABLE = False  # Local-only - contains private key material
PROJECTION_TABLE = None

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_unsafe_db, create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Connection pubkeys expire after 30 days (in milliseconds)
CONNECTION_PUBKEY_TTL_MS = 30 * 24 * 60 * 60 * 1000


# v2 event specification - no signer, no deps (local-only unsigned)
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for connection_pubkey events."""
    event_data = ctx.event_data

    public_key_b64 = event_data.get('public_key')
    private_key_b64 = event_data.get('private_key')
    owner_peer_id = event_data.get('signed_by')  # signed_by is the owner
    created_at = event_data.get('created_at')

    if not all([public_key_b64, private_key_b64, owner_peer_id, created_at is not None]):
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_connection_pubkey(public_key_b64, private_key_b64, owner_peer_id)

    # Decode keys from base64 to bytes
    public_key = crypto.b64decode(public_key_b64)
    private_key = crypto.b64decode(private_key_b64)

    # Calculate TTL
    ttl_ms = created_at + CONNECTION_PUBKEY_TTL_MS

    writes = (
        WriteOp(
            op='insert',
            table='connection_pubkeys',
            values={
                'connection_pubkey_id': ctx.event_id,
                'owner_peer_id': owner_peer_id,
                'public_key': public_key,
                'private_key': private_key,
                'created_at': created_at,
                'ttl_ms': ttl_ms,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)


def _wire_shadow_connection_pubkey(public_key_b64: str, private_key_b64: str, owner_peer_id: str) -> None:
    """Validate connection_pubkey fields against the fixed-size wire payload layout."""
    plaintext = wire_format.encode_connection_pubkey_plaintext(
        public_key=crypto.b64decode(public_key_b64),
        private_key=crypto.b64decode(private_key_b64),
    )
    decoded = wire_format.decode_connection_pubkey_plaintext(plaintext)
    if decoded["public_key"] != crypto.b64decode(public_key_b64):
        raise ValueError("wire shadow decode public_key mismatch")


# Pubkey replenishment configuration
MIN_CONNECTION_PUBKEYS = 10  # Minimum number of non-expired pubkeys to maintain
REPLENISH_CONNECTION_PUBKEYS = 20  # Number to generate when below minimum


def generate_batch(peer_id: str, count: int, t_ms: int, db: Any) -> list[str]:
    """Generate N connection pubkeys at once.

    Convenience function that calls create() N times with incremented timestamps.

    Args:
        peer_id: Local peer ID creating the pubkeys
        count: Number of pubkeys to generate
        t_ms: Base timestamp (will be incremented for each pubkey)
        db: Database connection (caller handles commit)

    Returns:
        List of connection_pubkey_id's created
    """
    log.info(f"connection_pubkey.generate_batch() peer_id={peer_id[:20]}..., count={count}, t_ms={t_ms}")

    pubkey_ids = []
    for i in range(count):
        timestamp = t_ms + i
        pubkey_id, _ = create(peer_id, timestamp, db)
        pubkey_ids.append(pubkey_id)

    log.info(f"connection_pubkey.generate_batch() generated {len(pubkey_ids)} pubkeys")
    return pubkey_ids


def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a device-wide connection pubkey event.

    Generates Ed25519 keypair, stores both public and private keys in event.
    Projects to connection_pubkeys table with owner_peer_id.

    Args:
        peer_id: Local peer ID (owner of this pubkey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (pubkey_id, pubkey_private): The stored pubkey event ID and private key bytes
    """
    log.info(f"connection_pubkey.create() creating new pubkey for peer_id={peer_id}, t_ms={t_ms}")

    # Generate Ed25519 keypair for pubkey
    pubkey_private, pubkey_public = crypto.generate_keypair()

    _wire_shadow_connection_pubkey(
        crypto.b64encode(pubkey_public),
        crypto.b64encode(pubkey_private),
        peer_id,
    )

    blob = wire_format.encode_connection_pubkey_wire_event(
        public_key=pubkey_public,
        private_key=pubkey_private,
        signed_by_b64=peer_id,
        created_at_ms=t_ms,
    )

    unsafedb = create_unsafe_db(db)

    # Store the blob to get pubkey_id
    pubkey_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    log.info(f"connection_pubkey.create() generated pubkey_id={pubkey_id}")

    # Calculate TTL: absolute time when this pubkey expires
    ttl_ms = t_ms + CONNECTION_PUBKEY_TTL_MS

    # Insert into connection_pubkeys table with TTL
    unsafedb.execute(
        "INSERT OR IGNORE INTO connection_pubkeys (connection_pubkey_id, owner_peer_id, public_key, private_key, created_at, ttl_ms) VALUES (?, ?, ?, ?, ?, ?)",
        (pubkey_id, peer_id, pubkey_public, pubkey_private, t_ms, ttl_ms)
    )

    # Create recorded wrapper where peer sees itself
    from core import recorded
    recorded_id = recorded.create(pubkey_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    log.info(f"connection_pubkey.create() projected pubkey_id={pubkey_id}, ttl_ms={ttl_ms}")
    return pubkey_id, pubkey_private


def create_with_material(public_key: bytes, private_key: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create a connection pubkey event with provided key material (for invite pubkeys).

    Args:
        public_key: Ed25519 public key bytes
        private_key: Ed25519 private key bytes
        peer_id: Local peer ID (owner of this pubkey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        pubkey_id: The stored pubkey event ID
    """
    log.info(f"connection_pubkey.create_with_material() creating pubkey for peer_id={peer_id}, t_ms={t_ms}")

    _wire_shadow_connection_pubkey(
        crypto.b64encode(public_key),
        crypto.b64encode(private_key),
        peer_id,
    )

    blob = wire_format.encode_connection_pubkey_wire_event(
        public_key=public_key,
        private_key=private_key,
        signed_by_b64=peer_id,
        created_at_ms=t_ms,
    )

    unsafedb = create_unsafe_db(db)

    # Store the blob to get pubkey_id
    pubkey_id = store.blob(blob, t_ms, return_dupes=True, unsafedb=unsafedb)
    log.info(f"connection_pubkey.create_with_material() generated pubkey_id={pubkey_id}")

    # Calculate TTL: absolute time when this pubkey expires
    ttl_ms = t_ms + CONNECTION_PUBKEY_TTL_MS

    # Insert into connection_pubkeys table with TTL
    unsafedb.execute(
        "INSERT OR IGNORE INTO connection_pubkeys (connection_pubkey_id, owner_peer_id, public_key, private_key, created_at, ttl_ms) VALUES (?, ?, ?, ?, ?, ?)",
        (pubkey_id, peer_id, public_key, private_key, t_ms, ttl_ms)
    )

    # Create recorded wrapper where peer sees itself
    from core import recorded
    recorded_id = recorded.create(pubkey_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    log.info(f"connection_pubkey.create_with_material() projected pubkey_id={pubkey_id}, ttl_ms={ttl_ms}")
    return pubkey_id


def get_connection_pubkey_for_peer(peer_shared_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the connection pubkey for a specific peer in format expected by crypto.wrap().

    Pubkeys are public and meant to be shared for encryption, indexed by peer_shared_id
    (public identity) rather than local peer_id.

    Args:
        peer_shared_id: Peer's peer_shared_id (public identity) to get pubkey for
        recorded_by: Local peer_id requesting access (for subjective view)
        db: Database connection

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'type': 'asymmetric'}
        or None if pubkey not found
    """
    import logging
    lookup_log = logging.getLogger(__name__)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    lookup_log.info(f"get_connection_pubkey_for_peer() looking for pubkey of peer_shared_id={peer_shared_id[:20]}..., requested_by={recorded_by[:20]}...")

    # Debug: show all connection_pubkeys_shared for this recorded_by
    all_shared = safedb.query("SELECT peer_id, connection_pubkey_id, connection_pubkey_shared_id FROM connection_pubkeys_shared WHERE recorded_by = ?", (recorded_by,))
    lookup_log.info(f"get_connection_pubkey_for_peer() ALL connection_pubkeys_shared for {recorded_by[:20]}: {[(r['peer_id'][:20], r['connection_pubkey_id'][:20] if r['connection_pubkey_id'] else 'NULL', r['connection_pubkey_shared_id'][:20]) for r in all_shared[:5]]}")

    result = safedb.query_one(
        "SELECT connection_pubkey_shared_id, connection_pubkey_id, public_key FROM connection_pubkeys_shared WHERE peer_id = ? AND recorded_by = ? ORDER BY created_at DESC, connection_pubkey_shared_id DESC LIMIT 1",
        (peer_shared_id, recorded_by)
    )

    if not result:
        # Fallback: check invite_accepteds for bootstrap case (inviter's connection pubkey from invite link)
        result = safedb.query_one(
            """SELECT inviter_connection_pubkey_id as connection_pubkey_id,
                      inviter_connection_pubkey_public_key as public_key
               FROM invite_accepteds
               WHERE inviter_peer_shared_id = ? AND recorded_by = ?
               AND inviter_connection_pubkey_public_key IS NOT NULL
               LIMIT 1""",
            (peer_shared_id, recorded_by)
        )
        if result:
            lookup_log.info(f"get_connection_pubkey_for_peer() FOUND via invite_accepteds for peer_shared_id={peer_shared_id[:20]}...")

    if not result:
        lookup_log.warning(f"get_connection_pubkey_for_peer() NO PUBKEY FOUND for peer_shared_id={peer_shared_id[:20]}...")
        return None

    # Use connection_pubkey_id as the crypto hint (matches pubkey_id in recipient's connection_pubkeys table)
    connection_pubkey_id_bytes = crypto.b64decode(result['connection_pubkey_id'])
    lookup_log.info(f"get_connection_pubkey_for_peer() FOUND connection_pubkey_id={result['connection_pubkey_id'][:20]}... for peer_shared_id={peer_shared_id[:20]}...")

    return {
        'id': connection_pubkey_id_bytes,
        'public_key': result['public_key'],
        'type': 'asymmetric'
    }


def replenish_for_all_peers(t_ms: int, db: Any) -> dict[str, Any]:
    """Replenish connection pubkeys for all local peers if running low.

    This is a recurring job that should run periodically (every 1-6 hours)
    to ensure each peer has enough non-expired pubkeys available.

    For each local peer:
    1. Count non-expired connection pubkeys
    2. If count < MIN_CONNECTION_PUBKEYS, generate REPLENISH_CONNECTION_PUBKEYS new ones

    Args:
        t_ms: Current time in milliseconds
        db: Database connection

    Returns:
        Dict with stats: {
            'peers_processed': int,
            'peers_replenished': int,
            'total_pubkeys_generated': int,
            'errors': list[str]
        }
    """
    log.info(f"connection_pubkey.replenish_for_all_peers() t_ms={t_ms}")

    unsafedb = create_unsafe_db(db)

    stats = {
        'peers_processed': 0,
        'peers_replenished': 0,
        'total_pubkeys_generated': 0,
        'errors': []
    }

    # Get all local peers
    local_peer_rows = unsafedb.query("SELECT peer_id FROM local_peers")

    if not local_peer_rows:
        log.info(f"connection_pubkey.replenish_for_all_peers() no local peers found")
        return stats

    log.info(f"connection_pubkey.replenish_for_all_peers() found {len(local_peer_rows)} local peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']
        try:
            # Count non-expired pubkeys for this peer
            pubkey_count_row = unsafedb.query_one(
                """SELECT COUNT(*) as count FROM connection_pubkeys
                   WHERE owner_peer_id = ? AND ttl_ms > ?""",
                (peer_id, t_ms)
            )
            pubkey_count = pubkey_count_row['count'] if pubkey_count_row else 0

            log.debug(f"connection_pubkey.replenish_for_all_peers() peer {peer_id[:20]}... has {pubkey_count} non-expired pubkeys")

            stats['peers_processed'] += 1

            # Replenish if below minimum
            if pubkey_count < MIN_CONNECTION_PUBKEYS:
                log.info(f"connection_pubkey.replenish_for_all_peers() peer {peer_id[:20]}... has only {pubkey_count} pubkeys, replenishing with {REPLENISH_CONNECTION_PUBKEYS}")

                pubkey_ids = generate_batch(peer_id, REPLENISH_CONNECTION_PUBKEYS, t_ms, db)
                # Note: create() already handles projection internally

                stats['peers_replenished'] += 1
                stats['total_pubkeys_generated'] += len(pubkey_ids)
                log.info(f"connection_pubkey.replenish_for_all_peers() generated {len(pubkey_ids)} pubkeys for peer {peer_id[:20]}...")

        except Exception as e:
            error = f"Error processing peer {peer_id[:20]}...: {e}"
            log.error(f"connection_pubkey.replenish_for_all_peers() {error}")
            stats['errors'].append(error)
            continue

    log.info(f"connection_pubkey.replenish_for_all_peers() complete: {stats['peers_processed']} peers processed, {stats['peers_replenished']} replenished, {stats['total_pubkeys_generated']} pubkeys generated")
    return stats
