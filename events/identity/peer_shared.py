"""Peer shared event type (shareable public identity)."""
from typing import Any
import json
import logging
import crypto
import store
from events.identity import peer
from db import create_safe_db, create_unsafe_db

log = logging.getLogger(__name__)


def create(peer_id: str, t_ms: int, db: Any,
           invite_id: str,
           invite_private_key: bytes) -> str:
    """Create a shareable peer_shared event from a local peer.

    peer_shared is ALWAYS signed by an invite (mode=peer). This ensures every
    peer_shared is linked to a user_id via the invite chain.

    Args:
        peer_id: Local peer ID
        t_ms: Timestamp
        db: Database connection
        invite_id: Peer invite ID (required - from invite(mode=peer))
        invite_private_key: Invite private key for signing (required)

    Returns:
        peer_shared_id: The ID of the created peer_shared event
    """
    log.info(f"peer_shared.create() creating peer_shared for peer_id={peer_id}, t_ms={t_ms}, invite_id={invite_id[:20]}...")

    # Get peer's public key (always needed)
    public_key = peer.get_public_key(peer_id, peer_id, db)

    # Create event dict
    event_data = {
        'type': 'peer_shared',
        'public_key': crypto.b64encode(public_key),
        'peer_id': peer_id,  # Link back to local peer
        'created_at': t_ms
    }

    # Sign with invite key (links peer_shared to user via invite)
    event_data['invite_id'] = invite_id
    event_data['signed_by'] = invite_id
    signed_event = crypto.sign_event(event_data, invite_private_key)
    log.info(f"peer_shared.create() signed with invite key (signed_by={invite_id[:20]}...)")

    # Canonicalize to get deterministic blob
    blob = crypto.canonicalize_json(signed_event)

    # Store event with recorded wrapper and projection
    peer_shared_id = store.event(blob, peer_id, t_ms, db)

    log.info(f"peer_shared.create() created peer_shared_id={peer_shared_id}")
    return peer_shared_id


def project(peer_shared_id: str, recorded_by: str, recorded_at: int, db: Any) -> str | None:
    """Project peer_shared event into peers_shared and linked_peers tables.

    Phase 3: Two verification modes:
    1. Legacy (no invite_id): Self-signed, verify with public_key from event
    2. Invite-based (signed_by=invite_id): Verify with invite_pubkey, link to user
    """
    log.warning(f"[PEER_SHARED_PROJECT] peer_shared_id={peer_shared_id[:20]}..., recorded_by={recorded_by[:20]}...")

    unsafedb = create_unsafe_db(db)
    safedb = create_safe_db(db, recorded_by=recorded_by)

    # Get blob from store
    blob = store.get(peer_shared_id, unsafedb)
    if not blob:
        log.warning(f"peer_shared.project() blob not found for peer_shared_id={peer_shared_id}")
        return None

    # Parse JSON (signed plaintext)
    event_data = crypto.parse_json(blob)

    # Get public key from event (always needed for peers_shared table)
    public_key_b64 = event_data.get('public_key')
    if not public_key_b64:
        log.warning(f"peer_shared.project() missing public_key in event data")
        return None

    # Phase 3: Determine verification mode
    invite_id = event_data.get('invite_id')
    signed_by = event_data.get('signed_by')
    user_id = None  # Will be set if invite-based linking

    if invite_id and signed_by == invite_id:
        # Invite-based verification (Phase 3 uniform peer linking)
        # Get invite_pubkey from invites table or store blob
        invite_pubkey_bytes = None

        invite_row = safedb.query_one(
            "SELECT invite_pubkey, user_id FROM invites WHERE invite_id = ? AND recorded_by = ? LIMIT 1",
            (invite_id, recorded_by)
        )

        if invite_row:
            invite_pubkey_bytes = crypto.b64decode(invite_row['invite_pubkey'])
            user_id = invite_row['user_id']
        else:
            # Try store blob (bootstrap case)
            invite_blob = store.get(invite_id, unsafedb)
            if invite_blob:
                invite_data = crypto.parse_json(invite_blob)
                invite_pubkey_b64 = invite_data.get('invite_pubkey')
                if invite_pubkey_b64:
                    invite_pubkey_bytes = crypto.b64decode(invite_pubkey_b64)
                    user_id = invite_data.get('user_id')
                    log.info(f"peer_shared.project() got invite_pubkey from store blob (bootstrap case)")

        if not invite_pubkey_bytes:
            log.warning(f"peer_shared.project() invite_id={invite_id[:20]}... not available yet")
            return None

        if not crypto.verify_event(event_data, invite_pubkey_bytes):
            log.warning(f"peer_shared.project() signature verification failed using invite_pubkey")
            return None

        log.info(f"peer_shared.project() verified with invite_pubkey, user_id={user_id[:20] if user_id else 'None'}...")
    else:
        # Legacy self-signed verification
        public_key = crypto.b64decode(public_key_b64)
        if not crypto.verify_event(event_data, public_key):
            log.warning(f"peer_shared.project() signature verification failed for peer_shared_id={peer_shared_id}")
            return None
        log.info(f"peer_shared.project() verified self-signed (legacy mode)")

    # Insert into peers_shared table
    safedb.execute(
        """INSERT OR IGNORE INTO peers_shared
           (peer_shared_id, peer_id, public_key, created_at, recorded_by, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            peer_shared_id,
            event_data['peer_id'],
            event_data['public_key'],
            event_data['created_at'],
            recorded_by,
            recorded_at
        )
    )
    log.info(f"peer_shared.project() inserted into peers_shared: peer_shared_id={peer_shared_id}, owner_peer_id={event_data['peer_id']}, recorded_by={recorded_by}")

    # Phase 3: Insert into linked_peers if invite-based (links peer to user)
    if user_id:
        safedb.execute(
            """INSERT OR IGNORE INTO linked_peers
               (link_id, user_id, peer_id, linked_at, recorded_by)
               VALUES (?, ?, ?, ?, ?)""",
            (
                peer_shared_id,  # Use peer_shared_id as link_id
                user_id,
                peer_shared_id,  # peer_id in linked_peers is peer_shared_id
                recorded_at,
                recorded_by
            )
        )
        log.info(f"peer_shared.project() linked peer to user: peer_shared_id={peer_shared_id[:20]}..., user_id={user_id[:20]}...")

    # Insert into peer_self table (subjective mapping) if this is our own peer
    # ONLY update peer_self for invite-signed peer_shared (has user_id)
    # Self-signed peer_shared from bootstrap should NOT update peer_self to avoid convergence issues
    owner_peer_id = event_data['peer_id']
    if owner_peer_id == recorded_by and user_id:
        # Invite-based peer_shared: update peer_self with user_id
        # This makes peer_self the canonical source for "what user am I?"
        safedb.execute(
            "INSERT OR REPLACE INTO peer_self (peer_id, peer_shared_id, user_id, recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?)",
            (owner_peer_id, peer_shared_id, user_id, recorded_by, recorded_at)
        )
        log.info(f"peer_shared.project() inserted into peer_self with user_id: peer_id={owner_peer_id[:20]}..., peer_shared_id={peer_shared_id[:20]}..., user_id={user_id[:20]}..., recorded_by={recorded_by[:20]}...")
    elif owner_peer_id == recorded_by:
        # Self-signed peer_shared: skip peer_self update (bootstrap only, not canonical)
        log.info(f"peer_shared.project() skipping peer_self update for self-signed peer_shared (no user_id)")

    # Mark as valid for this peer
    safedb.execute(
        "INSERT OR IGNORE INTO valid_events (event_id, recorded_by) VALUES (?, ?)",
        (peer_shared_id, recorded_by)
    )

    return peer_shared_id


def get_public_key(peer_shared_id: str, recorded_by: str, db: Any) -> bytes:
    """Get public key for a peer_shared_id from the perspective of recorded_by."""
    safedb = create_safe_db(db, recorded_by=recorded_by)
    row = safedb.query_one(
        "SELECT public_key FROM peers_shared WHERE peer_shared_id = ? AND recorded_by = ? LIMIT 1",
        (peer_shared_id, recorded_by)
    )
    if not row:
        raise ValueError(f"peer_shared not found: {peer_shared_id} for peer {recorded_by}")
    # public_key is stored as base64 string
    return crypto.b64decode(row['public_key'])


def get_peer_id_for_signing(peer_shared_id: str, recorded_by: str, db: Any) -> str:
    """Get the local peer_id associated with a peer_shared_id for signing.

    Args:
        peer_shared_id: The public peer_shared ID
        recorded_by: Peer ID requesting access (for access control)
        db: Database connection

    Returns:
        Local peer_id for signing

    Raises:
        ValueError: If peer_shared not found or peer doesn't have access
    """
    unsafedb = create_unsafe_db(db)

    # Get the event blob
    blob = store.get(peer_shared_id, unsafedb)
    if not blob:
        raise ValueError(f"peer_shared not found: {peer_shared_id}")

    event_data = crypto.parse_json(blob)
    peer_id = event_data.get('peer_id')
    if not peer_id:
        raise ValueError(f"peer_id not found in peer_shared event: {peer_shared_id}")

    # Security: Only allow access if the requester owns this peer_shared_id
    if peer_id != recorded_by:
        raise ValueError(f"access denied: peer {recorded_by} cannot access signing info for peer_shared {peer_shared_id}")

    return peer_id
