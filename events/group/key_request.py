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

    requested_secret_id = event_data.get('requested_secret_id')
    removal_epoch_id = event_data.get('removal_epoch_id')  # Can be None
    signed_by = event_data.get('signed_by')
    created_at = event_data.get('created_at')

    if not requested_secret_id or not signed_by or created_at is None:
        return ProjectorResult(writes=tuple(), valid_event=False)

    writes = (
        WriteOp(
            op='insert',
            table='key_requests',
            values={
                'request_id': ctx.event_id,
                'requested_secret_id': requested_secret_id,
                'removal_epoch_id': removal_epoch_id,
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
                'requested_secret_id': requested_secret_id,
                'removal_epoch_id': removal_epoch_id,
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

    requested_secret_id = request['requested_secret_id']
    removal_epoch_id = request['removal_epoch_id']
    requester_peer_id = request['requester_peer_id']

    # Check if we have the secret
    from events.group import secret
    key_bytes = secret.get_key_bytes(requested_secret_id, peer_id, db)
    if not key_bytes:
        log.info(f"key_request.fulfill_request() we don't have secret: {requested_secret_id}")
        return None

    # Check if the requester is still valid (not removed)
    if removal_epoch_id:
        from events.identity import removal_epoch
        if removal_epoch.is_removed(requester_peer_id, removal_epoch_id, peer_id, db):
            log.warning(f"key_request.fulfill_request() requester was removed: {requester_peer_id}")
            return None

    # Create secret_shared event for the requester
    from events.group import secret_shared
    try:
        secret_shared_id = secret_shared.create(
            secret_id=requested_secret_id,
            peer_id=peer_id,
            recipient_peer_id=requester_peer_id,
            removal_epoch_id=removal_epoch_id,
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


# Command handler for check_key_request_fulfillment
def _handle_check_key_request_fulfillment(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Command handler that checks if we can fulfill a key request."""
    request_id = args['request_id']
    fulfill_request(request_id, recorded_by, recorded_at, db)


# Register command handler
from core.projection.apply import register_command_handler
register_command_handler('check_key_request_fulfillment', _handle_check_key_request_fulfillment)
