"""Connection prekey event type (device-wide prekey keypair for receiving connection requests).

Renamed from transit_prekey for naming consistency with connection_request/connection_ack.
"""

# Registry metadata
EVENT_TYPE = 'connection_prekey'
SHAREABLE = False  # Local-only - contains private key material
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x30  # TYPE_CONNECTION_PREKEY
WIRE_PLAINTEXT_SIZE = 344  # CONNECTION_PREKEY_PLAINTEXT_SIZE
PUBKEY_SIZE = 32
PRIVKEY_SIZE = 32

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.db import create_unsafe_db, create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for connection_prekey event type

def encode_plaintext(public_key: bytes, private_key: bytes) -> bytes:
    """Encode a connection_prekey payload plaintext.

    Layout (344 bytes):
    - public_key (32)
    - private_key (32)
    - pad
    """
    wire_format._require_len("public_key", public_key, PUBKEY_SIZE)
    wire_format._require_len("private_key", private_key, PRIVKEY_SIZE)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:PUBKEY_SIZE] = public_key
    payload[PUBKEY_SIZE:PUBKEY_SIZE + PRIVKEY_SIZE] = private_key
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a connection_prekey payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(
            f"connection_prekey plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}"
        )
    return {
        "public_key": data[0:PUBKEY_SIZE],
        "private_key": data[PUBKEY_SIZE:PUBKEY_SIZE + PRIVKEY_SIZE],
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a connection_prekey wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(
    *,
    public_key: bytes,
    private_key: bytes,
    signed_by_b64: str,
    created_at_ms: int,
) -> bytes:
    """Encode a complete connection_prekey wire event."""
    signer_id = crypto.b64decode(signed_by_b64)
    plaintext = encode_plaintext(public_key=public_key, private_key=private_key)
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_UNSIGNED,
        signer_type=wire_format.SIGNER_PEER,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=wire_format._require_len("signer_id", signer_id, wire_format.SIGNER_ID_SIZE),
    )
    payload = wire_format._pad_payload(plaintext)
    signature = b"\x00" * wire_format.SIGNATURE_SIZE
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a connection_prekey wire event."""
    header, payload, _signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for connection_prekey")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    return {
        "type": EVENT_TYPE,
        "public_key": crypto.b64encode(decoded["public_key"]),
        "private_key": crypto.b64encode(decoded["private_key"]),
        "signed_by": crypto.b64encode(header.signer_id),
        "signer_type": wire_format.signer_type_to_str(header.signer_type),
        "created_at": header.created_at_ms,
    }


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

    _wire_shadow_connection_prekey(public_key_b64, private_key_b64, owner_peer_id)

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


def _wire_shadow_connection_prekey(public_key_b64: str, private_key_b64: str, owner_peer_id: str) -> None:
    """Validate connection_prekey fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        public_key=crypto.b64decode(public_key_b64),
        private_key=crypto.b64decode(private_key_b64),
    )
    decoded = decode_plaintext(plaintext)
    if decoded["public_key"] != crypto.b64decode(public_key_b64):
        raise ValueError("wire shadow decode public_key mismatch")


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

    _wire_shadow_connection_prekey(
        crypto.b64encode(prekey_public),
        crypto.b64encode(prekey_private),
        peer_id,
    )

    blob = encode_wire_event(
        public_key=prekey_public,
        private_key=prekey_private,
        signed_by_b64=peer_id,
        created_at_ms=t_ms,
    )

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
    from core import recorded
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

    _wire_shadow_connection_prekey(
        crypto.b64encode(public_key),
        crypto.b64encode(private_key),
        peer_id,
    )

    blob = encode_wire_event(
        public_key=public_key,
        private_key=private_key,
        signed_by_b64=peer_id,
        created_at_ms=t_ms,
    )

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
    from core import recorded
    recorded_id = recorded.create(prekey_id, peer_id, t_ms, db, return_dupes=False)
    recorded.project(recorded_id, db)

    log.info(f"connection_prekey.create_with_material() projected prekey_id={prekey_id}, ttl_ms={ttl_ms}")
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
