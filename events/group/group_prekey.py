"""Group prekey event type (subjective prekey for sealing group keys to members)."""

# Registry metadata
EVENT_TYPE = 'group_prekey'
SHAREABLE = False  # Local-only - contains private key material
PROJECTION_TABLE = None

# Wire format constants
WIRE_TYPE_CODE = 0x14  # TYPE_GROUP_PREKEY
WIRE_PLAINTEXT_SIZE = 344  # GROUP_PREKEY_PLAINTEXT_SIZE
PUBKEY_SIZE = 32
PRIVKEY_SIZE = 32

from typing import Any
import logging
import struct
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp

log = logging.getLogger(__name__)


# Wire format functions - encode/decode for group_prekey event type

def encode_plaintext(public_key: bytes, private_key: bytes) -> bytes:
    """Encode a group_prekey payload plaintext.

    Layout (344 bytes):
    - public_key (32)
    - private_key (32)
    - pad (280)
    """
    wire_format._require_len("public_key", public_key, PUBKEY_SIZE)
    wire_format._require_len("private_key", private_key, PRIVKEY_SIZE)
    payload = bytearray(WIRE_PLAINTEXT_SIZE)
    payload[0:PUBKEY_SIZE] = public_key
    payload[PUBKEY_SIZE:PUBKEY_SIZE + PRIVKEY_SIZE] = private_key
    return bytes(payload)


def decode_plaintext(data: bytes) -> dict[str, Any]:
    """Decode a group_prekey payload plaintext."""
    if len(data) != WIRE_PLAINTEXT_SIZE:
        raise ValueError(f"group_prekey plaintext must be {WIRE_PLAINTEXT_SIZE} bytes, got {len(data)}")
    return {
        "public_key": data[0:PUBKEY_SIZE],
        "private_key": data[PUBKEY_SIZE:PUBKEY_SIZE + PRIVKEY_SIZE],
    }


def is_wire_envelope(data: bytes) -> bool:
    """Check if data is a group_prekey wire envelope."""
    if len(data) != wire_format.WIRE_SIZE:
        return False
    try:
        header = wire_format.WireHeader.unpack(data[:wire_format.HEADER_SIZE])
    except ValueError:
        return False
    return header.version == 1 and header.event_type == WIRE_TYPE_CODE


def encode_wire_event(*, public_key: bytes, private_key: bytes, created_at_ms: int) -> bytes:
    """Encode a complete group_prekey wire event."""
    plaintext = encode_plaintext(public_key=public_key, private_key=private_key)
    header = wire_format.WireHeader(
        version=1,
        event_type=WIRE_TYPE_CODE,
        flags=wire_format.FLAG_UNSIGNED,
        signer_type=wire_format.SIGNER_NONE,
        count=0,
        created_at_ms=created_at_ms,
        ttl_ms=0,
        signer_id=b"\x00" * wire_format.SIGNER_ID_SIZE,
    )
    payload = wire_format._pad_payload(plaintext)
    signature = b"\x00" * wire_format.SIGNATURE_SIZE
    return wire_format.build_envelope(header, payload, signature)


def decode_wire_event(data: bytes) -> dict[str, Any]:
    """Decode a group_prekey wire event."""
    header, payload, _signature = wire_format.parse_envelope(data)
    if header.event_type != WIRE_TYPE_CODE:
        raise ValueError("unexpected event type for group_prekey")
    plaintext = payload[:WIRE_PLAINTEXT_SIZE]
    decoded = decode_plaintext(plaintext)
    return {
        "type": EVENT_TYPE,
        "public_key": crypto.b64encode(decoded["public_key"]),
        "private_key": crypto.b64encode(decoded["private_key"]),
        "created_at": header.created_at_ms,
    }


# Group prekeys expire after 30 days (in milliseconds)
GROUP_PREKEY_TTL_MS = 30 * 24 * 60 * 60 * 1000


# v2 event specification - no signer, no deps (local-only deterministic event)
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for group_prekey events.

    Group prekeys are local-only deterministic events (no timestamp in blob).
    Uses recorded_at as created_at and recorded_by as owner_peer_id.
    """
    event_data = ctx.event_data

    public_key_b64 = event_data.get('public_key')
    private_key_b64 = event_data.get('private_key')

    if not public_key_b64 or not private_key_b64:
        return ProjectorResult(writes=tuple(), valid_event=False)

    _wire_shadow_group_prekey(public_key_b64, private_key_b64)

    # Decode keys from base64 to bytes
    public_key = crypto.b64decode(public_key_b64)
    private_key = crypto.b64decode(private_key_b64)

    # Use recorded_at as created_at (deterministic blobs have no timestamp)
    created_at = ctx.recorded_at

    # Calculate TTL: absolute time when this prekey expires
    ttl_ms = created_at + GROUP_PREKEY_TTL_MS

    writes = (
        WriteOp(
            op='insert',
            table='group_prekeys',
            values={
                'prekey_id': ctx.event_id,
                'owner_peer_id': ctx.recorded_by,
                'public_key': public_key,
                'private_key': private_key,
                'created_at': created_at,
                'ttl_ms': ttl_ms,
                'recorded_by': ctx.recorded_by,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True)

# Prekey replenishment configuration
MIN_GROUP_PREKEYS = 10  # Minimum number of non-expired prekeys to maintain per peer
REPLENISH_GROUP_PREKEYS = 20  # Number to generate when below minimum


def generate_batch(peer_id: str, count: int, t_ms: int, db: Any) -> list[str]:
    """Generate N group prekeys at once.

    Convenience function that calls create() N times with incremented timestamps.

    Args:
        peer_id: Local peer ID creating the prekeys
        count: Number of prekeys to generate
        t_ms: Base timestamp (will be incremented for each prekey)
        db: Database connection (caller handles commit)

    Returns:
        List of group_prekey_id's created

    Example:
        >>> prekey_ids = generate_batch(alice_peer_id, count=5, t_ms=1000, db=db)
        >>> db.commit()
        >>> assert len(prekey_ids) == 5
    """
    log.info(f"group_prekey.generate_batch() peer_id={peer_id[:20]}..., count={count}, t_ms={t_ms}")

    prekey_ids = []
    for i in range(count):
        timestamp = t_ms + i
        prekey_id, _ = create(peer_id, timestamp, db)
        prekey_ids.append(prekey_id)

    log.info(f"group_prekey.generate_batch() generated {len(prekey_ids)} prekeys")
    return prekey_ids


def create(peer_id: str, t_ms: int, db: Any) -> tuple[str, bytes]:
    """Create a subjective group prekey event.

    Generates Ed25519 keypair, stores both public and private keys in event.
    Projects to group_prekeys table with recorded_by scoping.

    Args:
        peer_id: Local peer ID (owner of this prekey)
        t_ms: Timestamp
        db: Database connection

    Returns:
        (prekey_id, prekey_private): The stored prekey event ID and private key bytes
    """
    log.info(f"group_prekey.create() creating new prekey for peer_id={peer_id}, t_ms={t_ms}")

    # Generate Ed25519 keypair for prekey
    prekey_private, prekey_public = crypto.generate_keypair()

    # Create DETERMINISTIC event blob - only type and keys
    # This ensures same key material = same prekey_id on all peers
    blob = encode_wire_event(
        public_key=prekey_public,
        private_key=prekey_private,
        created_at_ms=0,
    )

    # Store the blob to get prekey_id
    prekey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"group_prekey.create() generated prekey_id={prekey_id}")

    return prekey_id, prekey_private


def create_from_material(public_key: bytes, private_key: bytes, peer_id: str, t_ms: int, db: Any) -> str:
    """Create group prekey event from provided key material (for invite prekeys).

    Creates a DETERMINISTIC group_prekey event from the key material.
    Same key material = same prekey_id on all peers.

    Args:
        public_key: The Ed25519 public key bytes
        private_key: The Ed25519 private key bytes
        peer_id: Peer ID that owns this prekey
        t_ms: Timestamp (used for recorded_at, NOT in the blob)
        db: Database connection

    Returns:
        prekey_id: The deterministic event ID
    """
    log.info(f"group_prekey.create_from_material() creating prekey for peer_id={peer_id}, t_ms={t_ms}")

    # Create DETERMINISTIC event blob - only type and keys
    # This ensures same key material = same prekey_id on all peers
    blob = encode_wire_event(
        public_key=public_key,
        private_key=private_key,
        created_at_ms=0,
    )

    # Store event - t_ms is used for recorded_at metadata, not in the blob itself
    prekey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"group_prekey.create_from_material() created prekey_id={prekey_id}")

    return prekey_id


def _wire_shadow_group_prekey(public_key_b64: str, private_key_b64: str) -> None:
    """Validate group_prekey fields against the fixed-size wire payload layout."""
    plaintext = encode_plaintext(
        public_key=crypto.b64decode(public_key_b64),
        private_key=crypto.b64decode(private_key_b64),
    )
    decoded = decode_plaintext(plaintext)
    if decoded["public_key"] != crypto.b64decode(public_key_b64):
        raise ValueError("wire shadow decode public_key mismatch")


def get_group_prekey_for_peer(peer_shared_id: str, recorded_by: str, db: Any) -> dict[str, Any] | None:
    """Get the group pre-key for a specific peer in format expected by crypto.wrap().

    Args:
        peer_shared_id: Peer's peer_shared_id (public identity) to get prekey for
        recorded_by: Local peer_id requesting access (for subjective view)
        db: Database connection

    Returns:
        Key dict with format {'id': bytes, 'public_key': bytes, 'type': 'asymmetric'}
        or None if prekey not found
    """
    safedb = create_safe_db(db, recorded_by=recorded_by)

    result = safedb.query_one(
        "SELECT group_prekey_id, public_key FROM group_prekeys_shared WHERE peer_id = ? AND recorded_by = ? ORDER BY created_at DESC LIMIT 1",
        (peer_shared_id, recorded_by)
    )

    if not result:
        return None

    # Use group_prekey_id as the hint (matches prekey_id in recipient's group_prekeys table)
    group_prekey_id_bytes = crypto.b64decode(result['group_prekey_id'])

    return {
        'id': group_prekey_id_bytes,
        'public_key': result['public_key'],
        'type': 'asymmetric'
    }


def replenish_for_all_peers(t_ms: int, db: Any) -> dict[str, Any]:
    """Replenish group prekeys for all local peers if running low.

    This is a recurring job that should run periodically (every 1-6 hours)
    to ensure each peer has enough non-expired prekeys available.

    For each local peer:
    1. Count non-expired group prekeys
    2. If count < MIN_GROUP_PREKEYS, generate REPLENISH_GROUP_PREKEYS new ones
    3. Create group_prekey_shared events so other peers can encrypt to them

    Args:
        t_ms: Current time in milliseconds
        db: Database connection

    Returns:
        Dict with stats: {
            'peers_processed': int,
            'peers_replenished': int,
            'total_prekeys_generated': int,
            'total_prekeys_shared': int,
            'errors': list[str]
        }

    Note: Each peer's prekeys are checked independently. If a peer has enough
    prekeys, no action is taken. Errors for one peer do not stop processing
    of other peers.
    """
    from events.group import group_prekey_shared

    log.info(f"group_prekey.replenish_for_all_peers() t_ms={t_ms}")

    from core.db import create_unsafe_db
    unsafedb = create_unsafe_db(db)

    stats = {
        'peers_processed': 0,
        'peers_replenished': 0,
        'total_prekeys_generated': 0,
        'total_prekeys_shared': 0,
        'errors': []
    }

    # Get all local peers (with peer_shared_id from device-wide table)
    local_peer_rows = unsafedb.query("SELECT peer_id, peer_shared_id FROM local_peers")

    if not local_peer_rows:
        log.info(f"group_prekey.replenish_for_all_peers() no local peers found")
        return stats

    log.info(f"group_prekey.replenish_for_all_peers() found {len(local_peer_rows)} local peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']
        peer_shared_id = peer_row['peer_shared_id']
        if not peer_shared_id:
            log.debug(f"group_prekey.replenish_for_all_peers() no peer_shared_id in local_peers for {peer_id[:20]}...")
            continue

        try:
            # Count non-expired group prekeys for this peer
            safedb = create_safe_db(db, recorded_by=peer_id)
            prekey_count_row = safedb.query_one(
                """SELECT COUNT(*) as count FROM group_prekeys
                   WHERE recorded_by = ? AND ttl_ms > ?""",
                (peer_id, t_ms)
            )
            prekey_count = prekey_count_row['count'] if prekey_count_row else 0

            log.debug(f"group_prekey.replenish_for_all_peers() peer {peer_id[:20]}... has {prekey_count} non-expired prekeys")

            stats['peers_processed'] += 1

            # Replenish if below minimum
            if prekey_count < MIN_GROUP_PREKEYS:
                log.info(f"group_prekey.replenish_for_all_peers() peer {peer_id[:20]}... has only {prekey_count} prekeys, replenishing with {REPLENISH_GROUP_PREKEYS}")

                prekey_ids = generate_batch(peer_id, REPLENISH_GROUP_PREKEYS, t_ms, db)

                # Project each prekey and create shared version
                for i, prekey_id in enumerate(prekey_ids):
                    timestamp = t_ms + i
                    project(prekey_id, peer_id, timestamp, db)

                    # Create shareable prekey event so other peers can encrypt to us
                    try:
                        group_prekey_shared.create(
                            prekey_id=prekey_id,
                            peer_id=peer_id,
                            peer_shared_id=peer_shared_id,
                            t_ms=timestamp + 1,  # Offset to avoid collision
                            db=db,
                        )
                        stats['total_prekeys_shared'] += 1
                    except Exception as share_error:
                        log.warning(f"group_prekey.replenish_for_all_peers() failed to share prekey {prekey_id[:20]}...: {share_error}")

                stats['peers_replenished'] += 1
                stats['total_prekeys_generated'] += len(prekey_ids)
                log.info(f"group_prekey.replenish_for_all_peers() generated {len(prekey_ids)} prekeys for peer {peer_id[:20]}...")

        except Exception as e:
            error = f"Error processing peer {peer_id[:20]}...: {e}"
            log.error(f"group_prekey.replenish_for_all_peers() {error}")
            stats['errors'].append(error)
            continue

    log.info(f"group_prekey.replenish_for_all_peers() complete: {stats['peers_processed']} peers processed, {stats['peers_replenished']} replenished, {stats['total_prekeys_generated']} prekeys generated, {stats['total_prekeys_shared']} shared")
    return stats


def share_existing_prekeys_for_all_peers(t_ms: int, db: Any) -> dict[str, Any]:
    """Share any existing group prekeys that haven't been shared yet.

    This is useful for ensuring prekeys created during join or other flows
    get shared with other network members.

    Args:
        t_ms: Current time in milliseconds
        db: Database connection

    Returns:
        Dict with stats: {
            'peers_processed': int,
            'total_prekeys_shared': int,
            'errors': list[str]
        }
    """
    from events.group import group_prekey_shared

    log.info(f"group_prekey.share_existing_prekeys_for_all_peers() t_ms={t_ms}")

    from core.db import create_unsafe_db
    unsafedb = create_unsafe_db(db)

    stats = {
        'peers_processed': 0,
        'total_prekeys_shared': 0,
        'errors': []
    }

    # Get all local peers (with peer_shared_id from device-wide table)
    local_peer_rows = unsafedb.query("SELECT peer_id, peer_shared_id FROM local_peers")

    if not local_peer_rows:
        return stats

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']
        peer_shared_id = peer_row['peer_shared_id']
        if not peer_shared_id:
            log.debug(f"share_existing_prekeys_for_all_peers() no peer_shared_id in local_peers for {peer_id[:20]}...")
            continue

        try:
            safedb = create_safe_db(db, recorded_by=peer_id)

            # Get all local prekeys that haven't been shared yet
            # Check which prekeys have corresponding shared events
            unshared_prekeys = safedb.query(
                """SELECT prekey_id, public_key FROM group_prekeys
                   WHERE owner_peer_id = ? AND recorded_by = ? AND ttl_ms > ?
                   AND prekey_id NOT IN (
                       SELECT group_prekey_id FROM group_prekeys_shared
                       WHERE recorded_by = ?
                   )""",
                (peer_id, peer_id, t_ms, peer_id)
            )

            stats['peers_processed'] += 1

            for i, prekey_row in enumerate(unshared_prekeys):
                try:
                    group_prekey_shared.create(
                        prekey_id=prekey_row['prekey_id'],
                        peer_id=peer_id,
                        peer_shared_id=peer_shared_id,
                        t_ms=t_ms + i,
                        db=db,
                    )
                    stats['total_prekeys_shared'] += 1
                except Exception as share_error:
                    log.warning(f"Failed to share prekey {prekey_row['prekey_id'][:20]}...: {share_error}")

        except Exception as e:
            error = f"Error processing peer {peer_id[:20]}...: {e}"
            log.error(f"group_prekey.share_existing_prekeys_for_all_peers() {error}")
            stats['errors'].append(error)
            continue

    log.info(f"group_prekey.share_existing_prekeys_for_all_peers() complete: {stats['peers_processed']} peers, {stats['total_prekeys_shared']} shared")
    return stats


def get_own_private_key(peer_id: str, db: Any) -> bytes | None:
    """Get the private key for one of the peer's own group prekeys.

    Used for signing (e.g., invite signatures).

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        Private key bytes or None if no prekey found
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    row = safedb.query_one(
        "SELECT private_key FROM group_prekeys WHERE owner_peer_id = ? AND recorded_by = ? LIMIT 1",
        (peer_id, peer_id)
    )
    return row['private_key'] if row else None


def list(peer_id: str, t_ms: int, db: Any) -> list[dict[str, Any]]:
    """List all group prekeys with status and group_key counts.

    Status:
    - active: has private key, not marked for purge
    - pending_purge: marked for purge (all group_keys purged)

    Args:
        peer_id: Local peer ID
        t_ms: Current time for TTL check
        db: Database connection

    Returns:
        List of dicts with: prekey_id, status, group_key_count, created_at
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    prekeys = safedb.query(
        """SELECT gp.prekey_id, gp.created_at,
                  CASE WHEN ptp.prekey_id IS NOT NULL THEN 'pending_purge' ELSE 'active' END as status,
                  (SELECT COUNT(*) FROM group_keys_shared gks
                   WHERE gks.recipient_prekey_id = gp.prekey_id AND gks.recorded_by = ?) as group_key_count
           FROM group_prekeys gp
           LEFT JOIN prekeys_to_purge ptp ON gp.prekey_id = ptp.prekey_id AND ptp.recorded_by = gp.recorded_by
           WHERE gp.recorded_by = ? AND gp.owner_peer_id = ?
           ORDER BY gp.created_at DESC""",
        (peer_id, peer_id, peer_id)
    )

    return [row for row in prekeys]
