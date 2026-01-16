"""Invite accepted event type (local-only, captures invite acceptance)."""

# Registry metadata
EVENT_TYPE = 'invite_accepted'
SHAREABLE = False  # Local-only - captures out-of-band invite data
EPHEMERAL = False
PROJECTION_TABLE = None

from typing import Any
import base64
import json
import logging
from core import crypto
from core import store
from core.db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


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


def project(invite_accepted_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project invite_accepted: establish trust anchor for network join.

    This is the trust anchor for the network join. It:
    1. Marks network_id as valid (TRUST ANCHOR) - unblocks bootstrap invite
    2. Stores inviter's peer_shared from link data (enabling sync)
    3. Stores connection metadata (ip/port)
    4. The cascade then naturally flows: network -> invite -> user -> etc.

    The invite itself syncs normally like all other events.

    Returns:
        invite_accepted_id on success, None on failure
    """
    log.info(f"[INVITE_ACCEPTED_PROJECT] id={invite_accepted_id[:20]}..., recorded_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(invite_accepted_id, unsafedb)
    if not blob:
        log.warning(f"invite_accepted.project() blob not found")
        return None

    event_data = crypto.parse_json(blob)

    # Extract invite link data
    invite_link_data = event_data['invite_link_data']

    invite_id = invite_link_data['invite_id']
    network_id = invite_link_data.get('network_id')
    inviter_peer_shared_id = invite_link_data.get('inviter_peer_shared_id')

    # =========================================================================
    # 1. MARK NETWORK_ID AS VALID (TRUST ANCHOR)
    # This is the cascade trigger - unblocks events waiting on network_id
    # =========================================================================
    from events.network import recorded as recorded_module
    from core import queues

    if network_id:
        safedb.execute(
            "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
            (network_id, recorded_by)
        )
        log.info(f"[INVITE_ACCEPTED_PROJECT] marked network_id={network_id[:20]}... as valid (TRUST ANCHOR)")

        # Unblock events that were waiting for network_id
        # This triggers the cascade: network -> bootstrap invite -> user -> etc.
        unblocked_by_network = queues.blocked.notify_event_valid(network_id, recorded_by, safedb)
        if unblocked_by_network:
            log.info(f"invite_accepted.project() unblocked {len(unblocked_by_network)} events waiting for network_id")
            recorded_module.project_ids(unblocked_by_network, db)

    # =========================================================================
    # 2. STORE INVITER'S PEER_SHARED FROM LINK DATA
    # This allows Bob to know Alice for sync purposes
    # =========================================================================
    inviter_peer_shared_blob_b64 = invite_link_data.get('inviter_peer_shared_blob')

    if inviter_peer_shared_blob_b64 and inviter_peer_shared_id:
        # Decode from urlsafe base64
        padding = 4 - len(inviter_peer_shared_blob_b64) % 4
        if padding != 4:
            inviter_peer_shared_blob_b64 += '=' * padding
        inviter_peer_shared_blob = base64.urlsafe_b64decode(inviter_peer_shared_blob_b64)

        # Store the inviter's peer_shared blob
        stored_ps_id = store.blob(inviter_peer_shared_blob, recorded_at, True, unsafedb)
        log.info(f"[INVITE_ACCEPTED_PROJECT] stored inviter peer_shared blob, id={stored_ps_id[:20]}...")

        # Create recorded wrapper for the inviter's peer_shared so it can be projected
        ps_recorded_id = recorded_module.create(stored_ps_id, recorded_by, recorded_at, db, return_dupes=False)
        log.info(f"[INVITE_ACCEPTED_PROJECT] created recorded wrapper for inviter peer_shared")

    # =========================================================================
    # 3. MARK INVITE_ACCEPTED AS VALID
    # =========================================================================
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (invite_accepted_id, recorded_by)
    )

    # =========================================================================
    # 4. STORE CONNECTION METADATA IN invite_accepteds TABLE
    # =========================================================================
    address = invite_link_data.get('ip')
    port = invite_link_data.get('port')
    invite_private_key_b64 = invite_link_data.get('invite_private_key')
    invite_private_key = crypto.b64decode(invite_private_key_b64) if invite_private_key_b64 else None

    # Extract inviter's transit prekey for initial connection
    inviter_transit_prekey_id = invite_link_data.get('inviter_transit_prekey_id')
    inviter_transit_prekey_public_key = None
    if invite_link_data.get('inviter_transit_prekey_public_key'):
        inviter_transit_prekey_public_key = crypto.b64decode(invite_link_data['inviter_transit_prekey_public_key'])

    # Extract user_id for device linking (peer invites carry the user_id being linked to)
    link_user_id = invite_link_data.get('user_id')

    safedb.execute("""
        INSERT OR IGNORE INTO invite_accepteds
        (invite_id, inviter_peer_shared_id, address, port, network_id, user_id,
         inviter_transit_prekey_id, inviter_transit_prekey_public_key,
         invite_private_key, created_at, recorded_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        invite_id,
        inviter_peer_shared_id,
        address,
        port,
        network_id,
        link_user_id,
        inviter_transit_prekey_id,
        inviter_transit_prekey_public_key,
        invite_private_key,
        event_data['created_at'],
        recorded_by
    ))

    log.info(f"invite_accepted.project() completed for {recorded_by}")
    return invite_accepted_id
