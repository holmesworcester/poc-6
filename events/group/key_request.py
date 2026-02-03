"""TreeKEM key_request event type (request a secret from another peer).

Key requests are signed, shareable events that allow peers to request secrets
they don't have access to. When a peer receives a key_request for a secret
they have, they respond with a deterministic secret_shared event.

This enables:
1. Partition healing: Peers can request keys from other partitions after merge
2. Late joiners: New peers can request historical keys they need
"""

# Registry metadata
EVENT_TYPE = 'key_request'
SHAREABLE = True  # Syncs to peers
PROJECTION_TABLE = ('key_requests', 'request_id')

from typing import Any
import logging
from core import crypto
from core import store
from core import wire_format
from core.db import create_safe_db
from core.projection.types import ProjectorResult, WriteOp, Command

log = logging.getLogger(__name__)


# Event specification - signed, shareable
EVENT_SPEC = {
    'encrypted': False,
    'signer': {
        'id_field': 'signed_by',
        'type_field': 'signer_type',
    },
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for key_request events.

    Records the request and triggers response handling if we have the secret.
    """
    event_data = ctx.event_data

    requested_key_id = event_data.get('requested_key_id')
    requester_pubkey_id = event_data.get('requester_pubkey_id')
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')

    if not requested_key_id or not requester_pubkey_id or not signed_by or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='key_requests',
            values={
                'request_id': ctx.event_id,
                'requested_key_id': requested_key_id,
                'requester_pubkey_id': requester_pubkey_id,
                'requester_peer_id': signed_by,
                'created_at': created_at,
                'recorded_at': ctx.recorded_at,
                'fulfilled': 0,  # Not yet fulfilled
            },
        ),
    )

    # Trigger command to check if we can fulfill this request
    commands = (
        Command(
            command_type='check_key_request_fulfillment',
            args={
                'request_id': ctx.event_id,
                'requested_key_id': requested_key_id,
                'requester_pubkey_id': requester_pubkey_id,
                'requester_peer_id': signed_by,
            },
        ),
    )

    return ProjectorResult(writes=writes, valid_event=True, commands=commands)


def create(peer_id: str, peer_shared_id: str, requested_key_id: str,
           requester_pubkey_id: str, t_ms: int, db: Any) -> str:
    """Create a key request for a key we don't have.

    Args:
        peer_id: Local peer ID (for signing)
        peer_shared_id: Public peer ID (signer identity)
        requested_key_id: The key ID we want (secret or other key)
        requester_pubkey_id: Our pubkey ID for the response to be encrypted to
        t_ms: Timestamp
        db: Database connection

    Returns:
        request_id: The event ID
    """
    log.info(f"key_request.create() requesting key={requested_key_id}, pubkey={requester_pubkey_id}, t_ms={t_ms}")

    # Get private key for signing
    from events.identity import peer
    private_key = peer.get_private_key(peer_id, peer_id, db)

    # Create signed wire event
    blob = wire_format.encode_key_request_wire_event(
        requested_key_id_b64=requested_key_id,
        requester_pubkey_id_b64=requester_pubkey_id,
        signed_by_b64=peer_shared_id,
        signer_type="peer_shared",
        created_at_ms=t_ms,
        private_key=private_key,
    )

    # Store event
    request_id = store.event(blob, peer_id, t_ms, db)
    log.info(f"key_request.create() created request_id={request_id}")

    return request_id


def fulfill_request(request_id: str, peer_id: str, t_ms: int, db: Any) -> str | None:
    """Attempt to fulfill a key request by creating a secret_shared event.

    DEDUPLICATION: Only the key owner (announcer) fulfills requests. This prevents
    duplicate responses when multiple peers have the key. We check if our peer_shared_id
    matches the signed_by field of the key_announce event.

    SECURITY: Keys are bound to removal_epochs. Before fulfilling a request, we check
    if the requester is removed in the key's removal_epoch. This ensures that:
    - If we have the key, we must have received its removal_epoch
    - A removed user cannot obtain keys created after their removal
    - This works even if we don't "know" we're denying a removed user - if we have
      the key, we have its epoch, which contains the removal

    Args:
        request_id: The key request ID
        peer_id: Local peer ID
        t_ms: Timestamp
        db: Database connection

    Returns:
        secret_shared_id if fulfilled, None otherwise
    """
    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get the request
    request = safedb.query_one(
        "SELECT * FROM key_requests WHERE request_id = ? AND recorded_by = ?",
        (request_id, peer_id)
    )

    if not request:
        log.warning(f"key_request.fulfill_request() request not found: {request_id}")
        return None

    if request['fulfilled']:
        log.info(f"key_request.fulfill_request() already fulfilled: {request_id}")
        return None

    requested_key_id = request['requested_key_id']
    requester_pubkey_id = request['requester_pubkey_id']
    requester_peer_id = request['requester_peer_id']

    # DEDUPLICATION: Only fulfill if we're the key owner (announcer)
    # This prevents duplicate responses from multiple peers
    announce = safedb.query_one(
        """SELECT signed_by FROM key_announces
           WHERE key_id = ? AND recorded_by = ?""",
        (requested_key_id, peer_id)
    )
    if not announce:
        log.info(f"key_request.fulfill_request() no key_announce found for key: {requested_key_id}")
        return None  # We don't know about this key announcement

    # Get our peer_shared_id
    peer_self = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (peer_id, peer_id)
    )
    if not peer_self or announce['signed_by'] != peer_self['peer_shared_id']:
        log.info(f"key_request.fulfill_request() we're not the owner, skipping: {requested_key_id}")
        return None  # We're not the owner, don't fulfill

    # Check if we have the secret
    from events.group import secret
    key_bytes = secret.get_key_bytes(requested_key_id, peer_id, db)
    if not key_bytes:
        log.info(f"key_request.fulfill_request() we don't have secret: {requested_key_id}")
        return None

    # SECURITY CHECK: Get the removal epoch for this key and check if requester is removed
    # This is the critical security property: keys created after a removal reference
    # that removal's epoch, so we can't share them with the removed user.
    from events.group import key_announce
    from events.identity import removal_epoch
    key_removal_epoch_id = key_announce.get_removal_epoch_for_key(requested_key_id, peer_id, db)

    if key_removal_epoch_id is not None:
        # Check if the requester is removed in this epoch
        if removal_epoch.is_removed(requester_peer_id, key_removal_epoch_id, peer_id, db):
            log.warning(
                f"key_request.fulfill_request() DENIED - requester {requester_peer_id[:20]}... "
                f"is removed in key's epoch {key_removal_epoch_id[:20]}..."
            )
            return None

    # Get requester's pubkey for wrapping
    from events.group import pubkey
    recipient_pubkey = pubkey.get_pubkey_by_id(requester_pubkey_id, peer_id, db)
    if not recipient_pubkey:
        log.warning(f"key_request.fulfill_request() requester pubkey not found: {requester_pubkey_id}")
        return None

    # Create secret_shared event for the requester
    from events.group import secret_shared
    try:
        secret_shared_id = secret_shared.create_to_pubkey(
            secret_id=requested_key_id,
            peer_id=peer_id,
            peer_shared_id=peer_id,  # Use peer_id as signer (we're local)
            recipient_pubkey_id=requester_pubkey_id,
            removal_epoch_id=key_removal_epoch_id,
            t_ms=t_ms,
            db=db
        )

        # Mark request as fulfilled
        safedb.execute(
            "UPDATE key_requests SET fulfilled = 1 WHERE request_id = ? AND recorded_by = ?",
            (request_id, peer_id)
        )

        log.info(f"key_request.fulfill_request() fulfilled request {request_id} with {secret_shared_id}")
        return secret_shared_id

    except Exception as e:
        log.warning(f"key_request.fulfill_request() failed to create secret_shared: {e}")
        return None


def get_pending_requests(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """Get all unfulfilled key requests.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of pending request records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT * FROM key_requests
           WHERE recorded_by = ? AND fulfilled = 0
           ORDER BY created_at ASC""",
        (peer_id,)
    ))


def list_requests(peer_id: str, db: Any) -> list[dict[str, Any]]:
    """List all key requests.

    Args:
        peer_id: Local peer ID
        db: Database connection

    Returns:
        List of request records
    """
    safedb = create_safe_db(db, recorded_by=peer_id)
    return list(safedb.query(
        """SELECT * FROM key_requests
           WHERE recorded_by = ?
           ORDER BY created_at DESC""",
        (peer_id,)
    ))


def request_missing_keys(peer_id: str, t_ms: int, db: Any) -> list[str]:
    """Request any missing announced keys for this peer.

    Scans for keys that have been announced (via key_announce) but we don't
    have the secret for. Creates key_request events for each missing key.

    Returns list of created request_ids.
    """
    from events.group import key_announce, pubkey

    safedb = create_safe_db(db, recorded_by=peer_id)

    # Get our identity
    peer_self = safedb.query_one(
        "SELECT peer_shared_id FROM peer_self WHERE peer_id = ? AND recorded_by = ?",
        (peer_id, peer_id)
    )
    if not peer_self:
        return []
    our_peer_shared_id = peer_self['peer_shared_id']

    # Get our pubkey for responses to be encrypted to
    our_pubkeys = pubkey.list_own_pubkeys(peer_id, db)
    if not our_pubkeys:
        log.info(f"key_request.request_missing_keys() no pubkeys found for peer {peer_id[:20]}...")
        return []
    # Use most recent pubkey
    our_pubkey_id = our_pubkeys[0]['pubkey_id']

    # Get all missing keys
    missing_keys = key_announce.get_all_missing_keys(peer_id, db)

    requested_ids = []
    for key_row in missing_keys:
        key_id = key_row['key_id']

        # Skip if already requested and not fulfilled
        existing = safedb.query_one(
            """SELECT 1 FROM key_requests
               WHERE requested_key_id = ? AND recorded_by = ? AND fulfilled = 0""",
            (key_id, peer_id)
        )
        if existing:
            continue

        # Create request
        request_id = create(
            peer_id=peer_id,
            peer_shared_id=our_peer_shared_id,
            requested_key_id=key_id,
            requester_pubkey_id=our_pubkey_id,
            t_ms=t_ms,
            db=db,
        )
        requested_ids.append(request_id)
        log.info(f"key_request.request_missing_keys() created request for key {key_id[:20]}...")

    return requested_ids


def handle_all_peers(t_ms: int, db: Any) -> dict[str, Any]:
    """Handle key requests for all local peers.

    Called by KeyRequestJob to scan all local peers for missing announced keys
    and create key_request events for them.

    Returns dict with 'requests_created' count.
    """
    from core.db import create_unsafe_db

    unsafedb = create_unsafe_db(db)
    peers = list(unsafedb.query("SELECT peer_id FROM local_peers"))

    total_requested = 0
    for peer_row in peers:
        peer_id = peer_row['peer_id']
        requested = request_missing_keys(peer_id, t_ms, db)
        total_requested += len(requested)

    return {'requests_created': total_requested}


# Command handler for check_key_request_fulfillment
def _handle_check_key_request_fulfillment(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Command handler that checks if we can fulfill a key request."""
    request_id = args['request_id']
    fulfill_request(request_id, recorded_by, recorded_at, db)


# Register command handler
from core.projection.apply import register_command_handler
register_command_handler('check_key_request_fulfillment', _handle_check_key_request_fulfillment)
