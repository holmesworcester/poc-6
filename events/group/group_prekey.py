"""Group prekey event type (subjective prekey for sealing group keys to members).

Pure functions:
    project(input_dict) -> ProjectorResult

API functions:
    generate_batch(peer_id, count, t_ms, db) -> list[str]
    create(peer_id, t_ms, db) -> tuple[str, bytes]
    project_event(prekey_id, recorded_by, recorded_at, db) -> None
    replenish_for_all_peers(t_ms, db) -> dict
"""
from typing import Any, TypedDict
import json
import logging
import crypto
import store
from db import create_safe_db

log = logging.getLogger(__name__)

# Group prekeys expire after 30 days (in milliseconds)
GROUP_PREKEY_TTL_MS = 30 * 24 * 60 * 60 * 1000

# Prekey replenishment configuration
MIN_GROUP_PREKEYS = 10
REPLENISH_GROUP_PREKEYS = 20


# ============================================================================
# TYPES
# ============================================================================

class GroupPrekeyEventData(TypedDict):
    type: str
    public_key: str  # base64
    private_key: str  # base64
    signed_by: str  # owner peer_id
    created_at: int


# ============================================================================
# SPEC - drives generic resolver
# ============================================================================

SPEC = {
    "encrypted": False,  # Plain JSON (local-only)
    "signer_type": "none",  # Not signed (local-only, contains private key)
    "dependencies": [],
    "tables": ["group_prekeys"],  # Subjective table (has recorded_by)
    "generic_dispatch": True,
    "mark_valid": True,
}


# ============================================================================
# PURE FUNCTIONS
# ============================================================================

def project(input_dict: dict):
    """Pure projection: dict -> result.

    Outputs group_prekeys row. Use apply_result() since group_prekeys is subjective.
    Calculates TTL from created_at + GROUP_PREKEY_TTL_MS.
    """
    from projection import ProjectorResult

    event_id = input_dict["event_id"]  # prekey_id
    event_data = input_dict["event_data"]
    recorded_by = input_dict["recorded_by"]

    public_key_b64 = event_data.get("public_key")
    private_key_b64 = event_data.get("private_key")
    owner_peer_id = event_data.get("signed_by")
    created_at = event_data.get("created_at")

    if not all([public_key_b64, private_key_b64, owner_peer_id, created_at is not None]):
        return ProjectorResult(valid=False, reason="missing required fields")

    # Decode keys (stored as bytes in DB)
    public_key = crypto.b64decode(public_key_b64)
    private_key = crypto.b64decode(private_key_b64)

    # Calculate TTL: absolute time when this prekey expires
    ttl_ms = created_at + GROUP_PREKEY_TTL_MS

    # Output: group_prekeys row (subjective table)
    row = {
        "prekey_id": event_id,
        "owner_peer_id": owner_peer_id,
        "public_key": public_key,
        "private_key": private_key,
        "created_at": created_at,
        "ttl_ms": ttl_ms,
        "recorded_by": recorded_by,
    }

    return ProjectorResult(
        valid=True,
        tables={"group_prekeys": [row]},
    )


# ============================================================================
# TEST BUILDERS
# ============================================================================

def make_event_data(
    public_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    private_key: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    signed_by: str = "peer_123",
    created_at: int = 1000000,
) -> dict:
    """Build event_data for testing."""
    return {
        "type": "group_prekey",
        "public_key": public_key,
        "private_key": private_key,
        "signed_by": signed_by,
        "created_at": created_at,
    }


def make_input(
    event_id: str = "prekey_123",
    event_data: dict | None = None,
    recorded_by: str = "peer_123",
    recorded_at: int = 1000001,
) -> dict:
    """Build complete input dict for testing."""
    return {
        "event_id": event_id,
        "event_data": event_data or make_event_data(),
        "recorded_by": recorded_by,
        "recorded_at": recorded_at,
        "dependencies": {},
    }


# ============================================================================
# API FUNCTIONS
# ============================================================================


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

    # Create event blob (plaintext JSON, no encryption for local-only)
    event_data = {
        'type': 'group_prekey',
        'public_key': crypto.b64encode(prekey_public),
        'private_key': crypto.b64encode(prekey_private),
        'signed_by': peer_id,  # Local peer who created this prekey
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store the blob to get prekey_id
    prekey_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"group_prekey.create() generated prekey_id={prekey_id}")

    return prekey_id, prekey_private


# project_event() handled by generic dispatch (SPEC.generic_dispatch = True)


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
        "SELECT group_prekey_shared_id, public_key FROM group_prekeys_shared WHERE peer_id = ? AND recorded_by = ? ORDER BY created_at DESC LIMIT 1",
        (peer_shared_id, recorded_by)
    )

    if not result:
        return None

    # Use group_prekey_shared_id as the hint/id for asymmetric keys
    group_prekey_shared_id_bytes = crypto.b64decode(result['group_prekey_shared_id'])

    return {
        'id': group_prekey_shared_id_bytes,
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
    log.info(f"group_prekey.replenish_for_all_peers() t_ms={t_ms}")

    from db import create_unsafe_db
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
        log.info(f"group_prekey.replenish_for_all_peers() no local peers found")
        return stats

    log.info(f"group_prekey.replenish_for_all_peers() found {len(local_peer_rows)} local peers")

    for peer_row in local_peer_rows:
        peer_id = peer_row['peer_id']
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

                # Project each prekey
                from projection import dispatch
                for i, prekey_id in enumerate(prekey_ids):
                    dispatch('group_prekey', prekey_id, peer_id, t_ms + i, db)

                stats['peers_replenished'] += 1
                stats['total_prekeys_generated'] += len(prekey_ids)
                log.info(f"group_prekey.replenish_for_all_peers() generated {len(prekey_ids)} prekeys for peer {peer_id[:20]}...")

        except Exception as e:
            error = f"Error processing peer {peer_id[:20]}...: {e}"
            log.error(f"group_prekey.replenish_for_all_peers() {error}")
            stats['errors'].append(error)
            continue

    log.info(f"group_prekey.replenish_for_all_peers() complete: {stats['peers_processed']} peers processed, {stats['peers_replenished']} replenished, {stats['total_prekeys_generated']} prekeys generated")
    return stats
