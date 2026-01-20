"""Invite accepted event type (local-only, captures invite acceptance)."""

# Registry metadata
EVENT_TYPE = 'invite_accepted'
SHAREABLE = False  # Local-only - captures out-of-band invite data
PROJECTION_TABLE = None

from typing import Any
import base64
import json
import logging
from core import crypto
from core import store
from core.db import create_safe_db, create_unsafe_db
from core.projection_v2.types import ProjectorResult, WriteOp, Command
from core.projection_v2.apply import register_command_handler

log = logging.getLogger(__name__)


# v2 event specification - local-only, unsigned (captures out-of-band invite data)
EVENT_SPEC = {
    'encrypted': False,
    'signer': None,  # Local-only, unsigned
    'requires': {},
    'optional': {},
    'cascade_on_delete': [],
}


def project_pure(ctx: Any) -> ProjectorResult:
    """Pure projector for invite_accepted events.

    invite_accepted is local-only and stores connection metadata from invite links.
    The engine handles marking network_id as valid (trust anchor).

    Writes to: invite_accepteds
    """
    event_data = ctx.event_data

    if event_data.get('type') != 'invite_accepted':
        return ProjectorResult(writes=tuple(), valid_event=False)

    invite_link_data = event_data.get('invite_link_data')
    if not invite_link_data:
        return ProjectorResult(writes=tuple(), valid_event=False)

    invite_id = invite_link_data.get('invite_id')
    if not invite_id:
        return ProjectorResult(writes=tuple(), valid_event=False)

    # Extract fields from invite_link_data
    inviter_peer_shared_id = invite_link_data.get('inviter_peer_shared_id')
    address = invite_link_data.get('ip')
    port = invite_link_data.get('port')
    network_id = invite_link_data.get('network_id')
    link_user_id = invite_link_data.get('user_id')
    inviter_connection_prekey_id = invite_link_data.get('inviter_connection_prekey_id')

    # Decode invite_private_key if present
    invite_private_key_b64 = invite_link_data.get('invite_private_key')
    invite_private_key = crypto.b64decode(invite_private_key_b64) if invite_private_key_b64 else None

    # Derive invite_pubkey from invite_private_key
    invite_pubkey_b64 = None
    if invite_private_key:
        from nacl.signing import SigningKey
        signing_key = SigningKey(invite_private_key)
        invite_pubkey_b64 = crypto.b64encode(bytes(signing_key.verify_key))

    # Decode inviter transit prekey if present
    inviter_connection_prekey_public_key = None
    if invite_link_data.get('inviter_connection_prekey_public_key'):
        inviter_connection_prekey_public_key = crypto.b64decode(invite_link_data['inviter_connection_prekey_public_key'])

    writes = [
        WriteOp(
            op='insert',
            table='invite_accepteds',
            values={
                'invite_id': invite_id,
                'inviter_peer_shared_id': inviter_peer_shared_id,
                'address': address,
                'port': port,
                'network_id': network_id,
                'user_id': link_user_id,
                'inviter_connection_prekey_id': inviter_connection_prekey_id,
                'inviter_connection_prekey_public_key': inviter_connection_prekey_public_key,
                'invite_private_key': invite_private_key,
                'invite_pubkey': invite_pubkey_b64,
                'created_at': event_data.get('created_at'),
                'recorded_by': ctx.recorded_by,
            },
        ),
    ]

    # Add trust anchor for network if present (device-wide table)
    if network_id:
        writes.append(WriteOp(
            op='insert',
            table='trust_anchors',
            values={
                'network_id': network_id,
                'recorded_by': ctx.recorded_by,
                'created_at': ctx.recorded_at,
            },
        ))

    # Command to handle network bootstrap (project network if in store, store inviter blob)
    commands = ()
    inviter_peer_shared_blob_b64 = invite_link_data.get('inviter_peer_shared_blob')
    if network_id or inviter_peer_shared_blob_b64:
        commands = (
            Command(
                command_type='handle_invite_accepted_bootstrap',
                args={
                    'network_id': network_id,
                    'inviter_peer_shared_id': inviter_peer_shared_id,
                    'inviter_peer_shared_blob_b64': inviter_peer_shared_blob_b64,
                }
            ),
        )

    return ProjectorResult(writes=tuple(writes), valid_event=True, commands=commands)


def create(invite_link_data: dict, peer_id: str, t_ms: int, db: Any) -> str:
    """Create local invite_accepted event (not shareable).

    This event captures the invite acceptance action and stores
    out-of-band data from the invite link for event-sourcing (reprojection).

    By storing the invite_link_data, we ensure that:
    - Full reprojection works without the original invite link
    - The projection system has all necessary data to restore state
    - The trust anchor (network_id) can be marked valid naturally

    Args:
        invite_link_data: Invite link data dictionary containing:
            - invite_id: ID of the invite event (syncs separately)
            - invite_private_key: For prekey and signing
            - invite_prekey_id: Crypto hint for prekey ID
            - network_id: Network being joined (trust anchor)
            - inviter_peer_shared_id: Inviter's peer_shared_id
            - inviter_peer_shared_blob: Inviter's peer_shared blob (base64 urlsafe)
            - ip/port: Inviter's address for connection
        peer_id: Bob's peer_id (local)
        t_ms: Timestamp
        db: Database connection

    Returns:
        invite_accepted_id: Event ID
    """
    log.info(f"invite_accepted.create() for invite={invite_link_data['invite_id']}, peer={peer_id}")

    event_data = {
        'type': 'invite_accepted',
        'invite_link_data': invite_link_data,
        'signed_by': peer_id,
        'created_at': t_ms
    }

    blob = json.dumps(event_data).encode()

    # Store with recorded wrapper and projection
    invite_accepted_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"invite_accepted.create() created invite_accepted_id={invite_accepted_id}")
    return invite_accepted_id


def _handle_invite_accepted_bootstrap(args: dict, recorded_by: str, recorded_at: int, db: Any) -> None:
    """Command handler for invite_accepted bootstrap side effects.

    1. Project network if blob is in store (trust anchor already set by write)
    2. Store inviter's peer_shared blob from link data
    """
    from events.network import recorded as recorded_module
    from events.identity import network as network_module
    from events import registry
    from core.projection_v2 import resolver as v2_resolver
    from core.projection_v2 import apply as v2_apply
    from core import queues

    safedb = create_safe_db(db, recorded_by=recorded_by)
    unsafedb = create_unsafe_db(db)

    network_id = args.get('network_id')
    inviter_peer_shared_id = args.get('inviter_peer_shared_id')
    inviter_peer_shared_blob_b64 = args.get('inviter_peer_shared_blob_b64')

    # Project network if blob is in store and not already projected
    if network_id:
        network_blob = store.get(network_id, unsafedb)
        if network_blob:
            already_projected = safedb.query_one(
                "SELECT 1 FROM networks WHERE network_id = ? AND recorded_by = ?",
                (network_id, recorded_by)
            )
            if not already_projected:
                # Use v2 projection path instead of legacy project()
                try:
                    network_data = crypto.parse_json(network_blob)
                except Exception as e:
                    log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] failed to parse network blob: {e}")
                    network_data = None

                if network_data:
                    resolve_result = v2_resolver.resolve_event(
                        ref_id=network_id,
                        event_type='network',
                        event_data=network_data,
                        recorded_by=recorded_by,
                        recorded_at=recorded_at,
                        db=db,
                    )

                    if resolve_result.status == 'ok' and resolve_result.ctx:
                        project_pure_fn = registry.get_project_pure_fn('network')
                        if project_pure_fn:
                            projector_result = project_pure_fn(resolve_result.ctx)
                            v2_apply.apply_writes(projector_result, recorded_by, recorded_at, db)

                            if projector_result.valid_event:
                                log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] projected network event {network_id[:20]}...")
                                # Mark network as valid
                                safedb.execute(
                                    "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
                                    (network_id, recorded_by)
                                )
                                # Notify blocked queue to unblock events waiting on network
                                unblocked_by_network = queues.blocked.notify_event_valid(network_id, recorded_by, safedb)
                                if unblocked_by_network:
                                    log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] unblocked {len(unblocked_by_network)} events after network became valid")
                                    # Re-project unblocked events recursively
                                    recorded_module.project_ids(unblocked_by_network, db)
                            else:
                                log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] network projection returned invalid for {network_id[:20]}...")
                    else:
                        log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] network resolve failed: {resolve_result.status} - {resolve_result.error}")
            else:
                log.debug(f"[INVITE_ACCEPTED_BOOTSTRAP] network already projected {network_id[:20]}...")

    # Store inviter's peer_shared blob from link data (for sync)
    if inviter_peer_shared_blob_b64 and inviter_peer_shared_id:
        padding = 4 - len(inviter_peer_shared_blob_b64) % 4
        if padding != 4:
            inviter_peer_shared_blob_b64 += '=' * padding
        inviter_peer_shared_blob = base64.urlsafe_b64decode(inviter_peer_shared_blob_b64)

        stored_ps_id = store.blob(inviter_peer_shared_blob, recorded_at, True, unsafedb)
        log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] stored inviter peer_shared blob, id={stored_ps_id[:20]}...")

        # Create recorded wrapper for inviter's peer_shared so it can be projected
        ps_recorded_id = recorded_module.create(stored_ps_id, recorded_by, recorded_at, db, return_dupes=False)
        log.warning(f"[INVITE_ACCEPTED_BOOTSTRAP] created recorded wrapper for inviter peer_shared")


# Register command handler at module load
register_command_handler('handle_invite_accepted_bootstrap', _handle_invite_accepted_bootstrap)
